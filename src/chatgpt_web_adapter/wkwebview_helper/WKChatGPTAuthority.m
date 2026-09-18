#import <Cocoa/Cocoa.h>
#import <WebKit/WebKit.h>
#include <fcntl.h>
#include <math.h>
#include <unistd.h>

static NSString * const WKResultPrefix = @"WK_RESULT ";

static NSString *ArgValue(NSArray<NSString *> *args, NSString *name, NSString *fallback) {
    NSUInteger idx = [args indexOfObject:name];
    if (idx == NSNotFound || idx + 1 >= args.count) return fallback;
    return args[idx + 1];
}

static NSArray<NSString *> *ArgValues(NSArray<NSString *> *args, NSString *name) {
    NSMutableArray<NSString *> *values = [NSMutableArray array];
    for (NSUInteger idx = 0; idx + 1 < args.count; idx += 1) {
        if ([args[idx] isEqualToString:name]) {
            [values addObject:args[idx + 1]];
            idx += 1;
        }
    }
    return values;
}

static BOOL HasArg(NSArray<NSString *> *args, NSString *name) {
    return [args containsObject:name];
}

static NSDictionary *ReadRequestEnvelope(void) {
    NSData *data = [[NSFileHandle fileHandleWithStandardInput] readDataToEndOfFile];
    if (data.length == 0) return @{};
    id parsed = [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
    return [parsed isKindOfClass:[NSDictionary class]] ? parsed : nil;
}

static NSString *RequestString(NSDictionary *request, NSString *key, NSString *fallback) {
    id value = request[key];
    return [value isKindOfClass:[NSString class]] ? value : fallback;
}

static BOOL RequestBool(NSDictionary *request, NSString *key, BOOL fallback) {
    id value = request[key];
    return [value isKindOfClass:[NSNumber class]] ? [value boolValue] : fallback;
}

static NSInteger RequestInteger(NSDictionary *request, NSString *key, NSInteger fallback) {
    id value = request[key];
    return [value isKindOfClass:[NSNumber class]] ? [value integerValue] : fallback;
}

static double RequestDouble(NSDictionary *request, NSString *key, double fallback) {
    id value = request[key];
    return [value isKindOfClass:[NSNumber class]] ? [value doubleValue] : fallback;
}

static NSArray<NSString *> *RequestStringArray(
    NSDictionary *request,
    NSString *key,
    NSArray<NSString *> *fallback
) {
    id value = request[key];
    if (![value isKindOfClass:[NSArray class]]) return fallback;
    NSMutableArray<NSString *> *items = [NSMutableArray array];
    for (id item in (NSArray *)value) {
        if (![item isKindOfClass:[NSString class]]) return fallback;
        [items addObject:item];
    }
    return items;
}

static NSString *Base64JSONValue(id value) {
    if (value == nil || value == [NSNull null]) return @"";
    if (![NSJSONSerialization isValidJSONObject:value]) return nil;
    NSData *data = [NSJSONSerialization dataWithJSONObject:value options:0 error:nil];
    return data != nil ? [data base64EncodedStringWithOptions:0] : nil;
}

static BOOL WriteUTF8ToFD(NSString *value, int fd) {
    if (fd < 0 || value.length == 0) return NO;
    NSData *data = [value dataUsingEncoding:NSUTF8StringEncoding];
    if (data.length == 0) return NO;
    const uint8_t *bytes = data.bytes;
    NSUInteger remaining = data.length;
    while (remaining > 0) {
        ssize_t written = write(fd, bytes, remaining);
        if (written <= 0) return NO;
        bytes += written;
        remaining -= (NSUInteger)written;
    }
    return YES;
}

static NSString *DecodeBase64(NSString *value) {
    if (value.length == 0) return @"";
    NSData *data = [[NSData alloc] initWithBase64EncodedString:value options:0];
    if (!data) return nil;
    return [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
}

static NSString *JSONStringLiteral(NSString *value) {
    NSData *data = [NSJSONSerialization dataWithJSONObject:(value ?: @"")
                                                   options:NSJSONWritingFragmentsAllowed
                                                     error:nil];
    NSString *json = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
    return json ?: @"\"\"";
}

static void RunLoopFor(NSTimeInterval seconds) {
    NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:seconds];
    while ([deadline timeIntervalSinceNow] > 0) {
        [[NSRunLoop mainRunLoop] runMode:NSDefaultRunLoopMode
                              beforeDate:[NSDate dateWithTimeIntervalSinceNow:0.05]];
    }
}

static id EvaluateSync(WKWebView *webView, NSString *script, NSTimeInterval timeout, NSError **outError) {
    __block BOOL done = NO;
    __block id result = nil;
    __block NSError *captured = nil;
    [webView evaluateJavaScript:script completionHandler:^(id value, NSError *error) {
        result = value;
        captured = error;
        done = YES;
    }];
    NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:timeout];
    while (!done && [deadline timeIntervalSinceNow] > 0) {
        [[NSRunLoop mainRunLoop] runMode:NSDefaultRunLoopMode
                              beforeDate:[NSDate dateWithTimeIntervalSinceNow:0.05]];
    }
    if (!done && captured == nil) {
        captured = [NSError errorWithDomain:@"WKChatGPTAuthority"
                                       code:1
                                   userInfo:@{NSLocalizedDescriptionKey:@"JavaScript evaluation timed out"}];
    }
    if (outError) *outError = captured;
    return result;
}

static NSDictionary *ParseJSONResult(id result) {
    if (![result isKindOfClass:[NSString class]]) return nil;
    NSData *data = [(NSString *)result dataUsingEncoding:NSUTF8StringEncoding];
    if (!data) return nil;
    id parsed = [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
    return [parsed isKindOfClass:[NSDictionary class]] ? parsed : nil;
}

static void PrintResult(NSDictionary *payload) {
    NSData *data = [NSJSONSerialization dataWithJSONObject:payload options:0 error:nil];
    NSString *json = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding] ?: @"{}";
    printf("%s%s\n", WKResultPrefix.UTF8String, json.UTF8String);
    fflush(stdout);
}

static void PrintEvent(NSDictionary *payload) {
    NSData *data = [NSJSONSerialization dataWithJSONObject:payload options:0 error:nil];
    NSString *json = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding] ?: @"{}";
    printf("WK_EVENT %s\n", json.UTF8String);
    fflush(stdout);
}

static void PrintEventForRequest(NSDictionary *payload, NSString *requestId) {
    if (requestId.length == 0) {
        PrintEvent(payload);
        return;
    }
    NSMutableDictionary *event = [payload mutableCopy];
    event[@"request_id"] = requestId;
    PrintEvent(event);
}

static NSString *ConversationIdFromURL(NSString *urlString) {
    if (![urlString isKindOfClass:[NSString class]]) return nil;
    NSRegularExpression *re = [NSRegularExpression regularExpressionWithPattern:@"/c/([^/?#]+)"
                                                                        options:0
                                                                          error:nil];
    NSTextCheckingResult *match = [re firstMatchInString:urlString
                                                 options:0
                                                   range:NSMakeRange(0, urlString.length)];
    if (!match || match.numberOfRanges < 2) return nil;
    NSString *value = [urlString substringWithRange:[match rangeAtIndex:1]];
    if (value.length == 0 || [value hasPrefix:@"WEB:"]) return nil;
    return value;
}

@interface WKAuthorityDelegate : NSObject <WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler>
@property(nonatomic, strong) WKWebView *webView;
@property(nonatomic, strong) NSWindow *window;
@property(nonatomic, strong) NSArray<NSString *> *attachmentPaths;
@property(nonatomic, assign) BOOL navigationFinished;
@property(nonatomic, assign) NSTimeInterval navigationFinishedAt;
@property(nonatomic, assign) NSUInteger chooserCount;
@property(nonatomic, strong) NSDictionary *canonicalResult;
@property(nonatomic, assign) BOOL canonicalDone;
@property(nonatomic, assign) BOOL submitRequestObserved;
@property(nonatomic, assign) BOOL submitTemporaryModeObserved;
@property(nonatomic, assign) BOOL submitProfileMatch;
@property(nonatomic, strong) NSString *submitModel;
@property(nonatomic, strong) NSString *submitThinkingEffort;
@property(nonatomic, strong) NSString *submitParentMessageId;
@property(nonatomic, assign) BOOL submitParentMatch;
@property(nonatomic, strong) NSString *submitEndpoint;
@property(nonatomic, assign) BOOL submitSignalPresent;
@property(nonatomic, assign) BOOL submitSignalAborted;
@property(nonatomic, assign) BOOL submitKeepalive;
@property(nonatomic, strong) NSString *submitRequestMode;
@property(nonatomic, assign) BOOL submitResponseObserved;
@property(nonatomic, assign) BOOL submitProxyDispatch;
@property(nonatomic, assign) NSInteger submitStatus;
@property(nonatomic, strong) NSString *submitError;
@property(nonatomic, assign) BOOL streamHandoffObserved;
@property(nonatomic, assign) BOOL streamHandoffReleased;
@property(nonatomic, assign) BOOL streamStarted;
@property(nonatomic, assign) BOOL streamResponseObserved;
@property(nonatomic, assign) NSInteger streamStatus;
@property(nonatomic, assign) BOOL streamEnded;
@property(nonatomic, assign) BOOL streamTerminalObserved;
@property(nonatomic, assign) BOOL streamGlobalCompletionObserved;
@property(nonatomic, assign) NSInteger streamRawEventCount;
@property(nonatomic, assign) NSInteger streamTextEventCount;
@property(nonatomic, assign) NSInteger streamLastSequence;
@property(nonatomic, strong) NSString *streamResumeToken;
@property(nonatomic, strong) NSString *streamTopicId;
@property(nonatomic, strong) NSString *streamTurnExchangeId;
@property(nonatomic, strong) NSString *streamConversationId;
@property(nonatomic, strong) NSString *streamStopConduitToken;
@property(nonatomic, strong) NSString *streamTurnTraceId;
@property(nonatomic, strong) NSString *streamClientMessageId;
@property(nonatomic, strong) NSString *streamAssistantMessageId;
@property(nonatomic, copy) NSString *brokerRequestId;
@end

@implementation WKAuthorityDelegate

- (void)webView:(WKWebView *)webView didFinishNavigation:(WKNavigation *)navigation {
    self.navigationFinished = YES;
    self.navigationFinishedAt = [NSDate timeIntervalSinceReferenceDate];
    PrintEventForRequest(@{@"type":@"navigation_finished"}, self.brokerRequestId);
}

- (void)webView:(WKWebView *)webView
runOpenPanelWithParameters:(WKOpenPanelParameters *)parameters
initiatedByFrame:(WKFrameInfo *)frame
completionHandler:(void (^)(NSArray<NSURL *> *URLs))completionHandler {
    self.chooserCount += 1;
    NSMutableArray<NSURL *> *urls = [NSMutableArray array];
    for (NSString *path in self.attachmentPaths ?: @[]) {
        [urls addObject:[NSURL fileURLWithPath:path]];
    }
    completionHandler(urls);
}

- (void)handleScriptMessageName:(NSString *)name body:(id)rawBody {
    if ([name isEqualToString:@"cwaCanonical"]) {
        if ([rawBody isKindOfClass:[NSDictionary class]]) {
            self.canonicalResult = (NSDictionary *)rawBody;
        } else {
            self.canonicalResult = @{@"ok":@NO,@"error":@"CANONICAL_READ_MESSAGE_INVALID"};
        }
        self.canonicalDone = YES;
        return;
    }
    if ([name isEqualToString:@"cwaStream"] && [rawBody isKindOfClass:[NSDictionary class]]) {
        NSDictionary *body = (NSDictionary *)rawBody;
        NSString *phase = [body[@"phase"] isKindOfClass:[NSString class]] ? body[@"phase"] : @"";
        if ([phase isEqualToString:@"started"]) {
            self.streamStarted = YES;
            NSNumber *status = [body[@"status"] isKindOfClass:[NSNumber class]] ? body[@"status"] : nil;
            if (status != nil) {
                self.streamResponseObserved = YES;
                self.streamStatus = [status integerValue];
            }
        }
        else if ([phase isEqualToString:@"ended"]) self.streamEnded = YES;
        else if ([phase isEqualToString:@"handoff_released"]) self.streamHandoffReleased = YES;
        else if ([phase isEqualToString:@"done"]) {
            self.streamEnded = YES;
            PrintEventForRequest(@{@"type":@"raw_ws_done"}, self.brokerRequestId);
        }
        else if ([phase isEqualToString:@"terminal"]) {
            self.streamTerminalObserved = YES;
            NSString *assistantMessageId = [body[@"message_id"] isKindOfClass:[NSString class]] ? body[@"message_id"] : nil;
            if (assistantMessageId.length > 0) self.streamAssistantMessageId = assistantMessageId;
        }
        else if ([phase isEqualToString:@"resume"]) {
            self.streamResumeToken = [body[@"token"] isKindOfClass:[NSString class]] ? body[@"token"] : nil;
            NSString *resumeConversationId = [body[@"conversation_id"] isKindOfClass:[NSString class]] ? body[@"conversation_id"] : nil;
            if (resumeConversationId.length > 0) self.streamConversationId = resumeConversationId;
            PrintEventForRequest(@{@"type":@"stream_resume_token_observed",@"token_present":@(self.streamResumeToken.length > 0),@"conversation_id_present":@(self.streamConversationId.length > 0)}, self.brokerRequestId);
        } else if ([phase isEqualToString:@"global_completion"]) {
            self.streamGlobalCompletionObserved = YES;
            NSString *completedConversationId = [body[@"conversation_id"] isKindOfClass:[NSString class]] ? body[@"conversation_id"] : nil;
            if (completedConversationId.length > 0) self.streamConversationId = completedConversationId;
            PrintEventForRequest(@{@"type":@"global_completion_observed",@"conversation_id_present":@(completedConversationId.length > 0)}, self.brokerRequestId);
        } else if ([phase isEqualToString:@"stop_context"]) {
            self.streamStopConduitToken = [body[@"conduit_token"] isKindOfClass:[NSString class]] ? body[@"conduit_token"] : nil;
            self.streamTurnTraceId = [body[@"turn_trace_id"] isKindOfClass:[NSString class]] ? body[@"turn_trace_id"] : nil;
        } else if ([phase isEqualToString:@"client_message"]) {
            self.streamClientMessageId = [body[@"message_id"] isKindOfClass:[NSString class]] ? body[@"message_id"] : nil;
        } else if ([phase isEqualToString:@"raw"]) {
            self.streamRawEventCount += 1;
            NSDictionary *parsed = [body[@"parsed"] isKindOfClass:[NSDictionary class]] ? body[@"parsed"] : nil;
            if (parsed != nil) {
                NSString *rawType = [parsed[@"type"] isKindOfClass:[NSString class]] ? parsed[@"type"] : @"";
                if ([rawType isEqualToString:@"message_stream_complete"]) {
                    self.streamTerminalObserved = YES;
                    NSString *completedConversationId = [parsed[@"conversation_id"] isKindOfClass:[NSString class]]
                        ? parsed[@"conversation_id"]
                        : nil;
                    if (completedConversationId.length > 0) {
                        self.streamConversationId = completedConversationId;
                    }
                }
                PrintEventForRequest(@{@"type":@"raw_ws_event",@"parsed":parsed}, self.brokerRequestId);
            }
        } else if ([phase isEqualToString:@"text"]) {
            self.streamTextEventCount += 1;
            NSString *eventType = [body[@"type"] isKindOfClass:[NSString class]] ? body[@"type"] : @"";
            if ([eventType isEqualToString:@"assistant_text_snapshot"]
                || [eventType isEqualToString:@"assistant_text_delta"]
                || [eventType isEqualToString:@"assistant_text_revision"]) {
                NSString *assistantMessageId = [body[@"message_id"] isKindOfClass:[NSString class]] ? body[@"message_id"] : nil;
                if (assistantMessageId.length > 0) self.streamAssistantMessageId = assistantMessageId;
                self.streamLastSequence += 1;
                NSMutableDictionary *event = [body mutableCopy];
                [event removeObjectForKey:@"phase"];
                event[@"sequence"] = @(self.streamLastSequence);
                PrintEventForRequest(event, self.brokerRequestId);
            }
        } else if ([phase isEqualToString:@"dom_text"]) {
            NSString *assistantMessageId = [body[@"message_id"] isKindOfClass:[NSString class]] ? body[@"message_id"] : nil;
            NSString *text = [body[@"text"] isKindOfClass:[NSString class]] ? body[@"text"] : nil;
            NSString *conversationId = [body[@"conversation_id"] isKindOfClass:[NSString class]] ? body[@"conversation_id"] : nil;
            if (conversationId.length > 0) self.streamConversationId = conversationId;
            if (assistantMessageId.length > 0 && text.length > 0) {
                self.streamAssistantMessageId = assistantMessageId;
                // Temporary protected writes have an authoritative proxy stream.
                // DOM innerText may contain transient layout newlines while React
                // is streaming, so do not let it rewrite Temporary proxy text.
                // Normal turns still use DOM revisions for their richer live path.
                if (!self.submitProxyDispatch || !self.submitTemporaryModeObserved) {
                    self.streamTextEventCount += 1;
                    self.streamLastSequence += 1;
                    PrintEventForRequest(@{
                        @"type": @"assistant_text_revision",
                        @"sequence": @(self.streamLastSequence),
                        @"message_id": assistantMessageId,
                        @"text": text
                    }, self.brokerRequestId);
                }
            }
        } else if ([phase isEqualToString:@"dom_terminal"]) {
            NSString *assistantMessageId = [body[@"message_id"] isKindOfClass:[NSString class]] ? body[@"message_id"] : nil;
            NSString *text = [body[@"text"] isKindOfClass:[NSString class]] ? body[@"text"] : nil;
            NSString *conversationId = [body[@"conversation_id"] isKindOfClass:[NSString class]] ? body[@"conversation_id"] : nil;
            if (conversationId.length > 0) self.streamConversationId = conversationId;
            if (assistantMessageId.length > 0 && text.length > 0) {
                self.streamAssistantMessageId = assistantMessageId;
                if (!self.submitProxyDispatch) {
                    self.streamTextEventCount += 1;
                    self.streamLastSequence += 1;
                    PrintEventForRequest(@{
                        @"type": @"assistant_text_revision",
                        @"sequence": @(self.streamLastSequence),
                        @"message_id": assistantMessageId,
                        @"text": text,
                        @"finish_reason": @"stop"
                    }, self.brokerRequestId);
                }
                // Keep DOM terminal as a passive finality fence even when proxy
                // text is authoritative; it must not rewrite the proxy text.
                self.streamTerminalObserved = YES;
            }
        } else if ([phase isEqualToString:@"handoff"]) {
            self.streamHandoffObserved = YES;
            NSString *handoffTopicId = [body[@"topic_id"] isKindOfClass:[NSString class]] ? body[@"topic_id"] : nil;
            NSString *handoffTurnExchangeId = [body[@"turn_exchange_id"] isKindOfClass:[NSString class]] ? body[@"turn_exchange_id"] : nil;
            NSString *handoffConversationId = [body[@"conversation_id"] isKindOfClass:[NSString class]] ? body[@"conversation_id"] : nil;
            if (handoffTurnExchangeId.length > 0) self.streamTurnExchangeId = handoffTurnExchangeId;
            if (handoffTopicId.length == 0 && self.streamTurnExchangeId.length > 0) {
                handoffTopicId = [@"conversation-turn-" stringByAppendingString:self.streamTurnExchangeId];
            }
            if (handoffTopicId.length > 0) self.streamTopicId = handoffTopicId;
            if (handoffConversationId.length > 0) self.streamConversationId = handoffConversationId;
        }
        return;
    }
    if ([name isEqualToString:@"cwaSubmit"] && [rawBody isKindOfClass:[NSDictionary class]]) {
        NSDictionary *body = (NSDictionary *)rawBody;
        NSString *phase = [body[@"phase"] isKindOfClass:[NSString class]] ? body[@"phase"] : @"";
        if ([phase isEqualToString:@"request"]) {
            self.submitRequestObserved = YES;
            self.submitTemporaryModeObserved = [body[@"temporary_mode"] boolValue];
            self.submitProfileMatch = [body[@"profile_match"] boolValue];
            self.submitModel = [body[@"model"] isKindOfClass:[NSString class]] ? body[@"model"] : @"";
            self.submitThinkingEffort = [body[@"thinking_effort"] isKindOfClass:[NSString class]] ? body[@"thinking_effort"] : @"";
            self.submitParentMessageId = [body[@"parent_message_id"] isKindOfClass:[NSString class]] ? body[@"parent_message_id"] : @"";
            self.submitParentMatch = [body[@"parent_match"] boolValue];
            self.submitEndpoint = [body[@"endpoint"] isKindOfClass:[NSString class]] ? body[@"endpoint"] : @"";
            self.submitSignalPresent = [body[@"signal_present"] boolValue];
            self.submitSignalAborted = [body[@"signal_aborted"] boolValue];
            self.submitKeepalive = [body[@"keepalive"] boolValue];
            self.submitRequestMode = [body[@"request_mode"] isKindOfClass:[NSString class]] ? body[@"request_mode"] : @"";
            PrintEventForRequest(@{
                @"type":@"submit_request_observed",
                @"endpoint":self.submitEndpoint ?: @"",
                @"temporary_mode":@(self.submitTemporaryModeObserved),
                @"profile_match":@(self.submitProfileMatch),
                @"model":self.submitModel ?: @"",
                @"thinking_effort":self.submitThinkingEffort ?: @"",
                @"parent_message_id":self.submitParentMessageId ?: @"",
                @"parent_match":@(self.submitParentMatch),
                @"reasoning_effort":[body[@"reasoning_effort"] isKindOfClass:[NSString class]] ? body[@"reasoning_effort"] : @"",
                @"effort":[body[@"effort"] isKindOfClass:[NSString class]] ? body[@"effort"] : @"",
                @"signal_present":@(self.submitSignalPresent),
                @"signal_aborted":@(self.submitSignalAborted),
                @"keepalive":@(self.submitKeepalive),
                @"request_mode":self.submitRequestMode ?: @""
            }, self.brokerRequestId);
        } else if ([phase isEqualToString:@"parent_error"]) {
            self.submitRequestObserved = YES;
            self.submitParentMatch = NO;
            self.submitParentMessageId = [body[@"parent_message_id"] isKindOfClass:[NSString class]]
                ? body[@"parent_message_id"]
                : @"";
            self.submitError = @"CWA_PARENT_MISMATCH";
        } else if ([phase isEqualToString:@"response"]) {
            self.submitRequestObserved = YES;
            self.submitResponseObserved = YES;
            NSNumber *status = [body[@"status"] isKindOfClass:[NSNumber class]] ? body[@"status"] : @0;
            self.submitStatus = status.integerValue;
            self.submitProxyDispatch = [body[@"proxy_dispatch"] boolValue];
            PrintEventForRequest(@{
                @"type":@"submit_response_observed",
                @"status":status,
                @"proxy_dispatch":@(self.submitProxyDispatch)
            }, self.brokerRequestId);
        } else if ([phase isEqualToString:@"error"]) {
            self.submitRequestObserved = YES;
            self.submitError = [body[@"error"] isKindOfClass:[NSString class]] ? body[@"error"] : @"SUBMIT_FETCH_FAILED";
        }
    }

}

- (void)userContentController:(WKUserContentController *)userContentController
      didReceiveScriptMessage:(WKScriptMessage *)message {
    [self handleScriptMessageName:message.name body:message.body];
}

@end

@interface WKTurnBrokerRouter : NSObject<WKScriptMessageHandler>
@property(nonatomic, strong) NSMutableDictionary<NSString *, WKAuthorityDelegate *> *delegates;
@end

@implementation WKTurnBrokerRouter
- (instancetype)init {
    self = [super init];
    if (self) self.delegates = [NSMutableDictionary dictionary];
    return self;
}
- (void)registerDelegate:(WKAuthorityDelegate *)delegate requestId:(NSString *)requestId {
    if (delegate == nil || requestId.length == 0) return;
    self.delegates[requestId] = delegate;
}
- (void)removeRequestId:(NSString *)requestId {
    if (requestId.length > 0) [self.delegates removeObjectForKey:requestId];
}
- (void)userContentController:(WKUserContentController *)userContentController
      didReceiveScriptMessage:(WKScriptMessage *)message {
    if (![message.body isKindOfClass:[NSDictionary class]]) return;
    NSDictionary *body = (NSDictionary *)message.body;
    NSString *requestId = [body[@"request_id"] isKindOfClass:[NSString class]] ? body[@"request_id"] : @"";
    if (requestId.length == 0) return;
    WKAuthorityDelegate *delegate = self.delegates[requestId];
    if (delegate == nil) return;
    if ([message.name isEqualToString:@"cwaProxyFetch"]) {
        NSMutableDictionary *event = [body mutableCopy];
        event[@"type"] = @"broker_proxy_fetch_request";
        PrintEvent(event);
        return;
    }
    NSMutableDictionary *forwarded = [body mutableCopy];
    [forwarded removeObjectForKey:@"request_id"];
    [delegate handleScriptMessageName:message.name body:forwarded];
}
@end

static NSString *PrivateTurnHandoffJSON(WKAuthorityDelegate *delegate) {
    NSDictionary *payload = @{
        @"v": @2,
        @"r": delegate.streamResumeToken ?: @"",
        @"p": delegate.streamTopicId ?: @"",
        @"x": delegate.streamTurnExchangeId ?: @"",
        @"i": delegate.streamConversationId ?: @"",
        @"c": delegate.streamStopConduitToken ?: @"",
        @"t": delegate.streamTurnTraceId ?: @""
    };
    NSData *data = [NSJSONSerialization dataWithJSONObject:payload options:0 error:nil];
    if (data.length == 0) return nil;
    return [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
}

static NSString *SubmitObservationScript(void) {
    return @"(()=>{"
            "if(window.__cwaSubmitObserverInstalled&&typeof window.__cwaRearmSubmitFetchObserver==='function')return window.__cwaRearmSubmitFetchObserver();window.__cwaSubmitObserverInstalled=true;"
            "const postedPhases=new Set();const post=(body,requestId=null)=>{try{const id=requestId||window.__CWA_BROKER_REQUEST_ID__;const phase=body&&typeof body.phase==='string'?body.phase:'';const key=id&&phase?String(id)+':'+phase:'';if(key&&postedPhases.has(key))return;if(key)postedPhases.add(key);if(id)body={...body,request_id:id};window.webkit.messageHandlers.cwaSubmit.postMessage(body)}catch(_){}};"
            "const proxyStates=new Map();"
            "const proxyError=(value)=>value instanceof Error?value:new TypeError(String(value||'WKWEBVIEW_PROXY_FETCH_FAILED'));"
            "window.__cwaProxyFetchHeaders=(id,status,headers)=>{const state=proxyStates.get(String(id||''));if(!state)return false;try{const response=new Response(state.stream,{status:Number(status)||200,headers:headers&&typeof headers==='object'?headers:{}});state.resolved=true;state.resolve(response);return true;}catch(error){state.reject(proxyError(error));proxyStates.delete(String(id||''));return false;}};"
            "window.__cwaProxyFetchChunk=(id,b64)=>{const state=proxyStates.get(String(id||''));if(!state||!state.controller)return false;try{const raw=atob(String(b64||''));const bytes=new Uint8Array(raw.length);for(let i=0;i<raw.length;i++)bytes[i]=raw.charCodeAt(i);state.controller.enqueue(bytes);return true;}catch(error){try{state.controller.error(proxyError(error))}catch(_){}proxyStates.delete(String(id||''));return false;}};"
            "window.__cwaProxyFetchEnd=(id)=>{const key=String(id||'');const state=proxyStates.get(key);if(!state)return false;try{if(state.controller)state.controller.close();else if(!state.resolved)state.reject(proxyError('WKWEBVIEW_PROXY_FETCH_ENDED_BEFORE_HEADERS'));}catch(_){}proxyStates.delete(key);return true;};"
            "window.__cwaProxyFetchError=(id,message)=>{const key=String(id||'');const state=proxyStates.get(key);if(!state)return false;const error=proxyError(message);try{if(state.resolved&&state.controller)state.controller.error(error);else state.reject(error);}catch(_){}proxyStates.delete(key);return true;};"
            "const proxyFetch=(input,init,requestId)=>new Promise((resolve,reject)=>{try{const proxyId=(globalThis.crypto&&typeof crypto.randomUUID==='function'?crypto.randomUUID():String(Date.now())+'-'+Math.random().toString(16).slice(2));let controller=null;const stream=new ReadableStream({start(value){controller=value;}});proxyStates.set(proxyId,{resolve,reject,controller,stream,resolved:false});const requestHeaders=[];try{for(const pair of new Headers((init&&init.headers)||(input instanceof Request?input.headers:undefined)).entries())requestHeaders.push(pair);}catch(_){}const rawUrl=input instanceof Request?input.url:String(input||'');const absoluteUrl=new URL(rawUrl,location.href).href;const method=String((init&&init.method)||(input instanceof Request?input.method:'POST')||'POST').toUpperCase();const rawBody=init&&Object.prototype.hasOwnProperty.call(init,'body')?init.body:(input instanceof Request?null:null);if(typeof rawBody!=='string')throw new TypeError('WKWEBVIEW_PROXY_FETCH_BODY_UNSUPPORTED');window.webkit.messageHandlers.cwaProxyFetch.postMessage({request_id:String(requestId||''),proxy_id:proxyId,url:absoluteUrl,method,headers:requestHeaders,body:rawBody,user_agent:String(navigator.userAgent||'')});}catch(error){reject(proxyError(error));}});"
            "const install=()=>{const originalFetch=window.fetch;if(typeof originalFetch!=='function')return false;if(originalFetch.__cwaIncludesSubmitObserver===true)return true;"
            "const wrapped=async function(input,init){"
              "const url=typeof input==='string'?input:((input&&input.url)||'');"
              "const method=((init&&init.method)||(input&&input.method)||'GET').toUpperCase();"
              "let watched=false,endpoint=null;try{const u=new URL(url,location.href);const p=u.pathname.replace(/\\/+$/,'');watched=method==='POST'&&u.origin===location.origin&&(p.endsWith('/backend-api/conversation')||p.endsWith('/backend-api/f/conversation')||p.endsWith('/backend-api/f/conversation/resume'));if(watched)endpoint=p;}catch(_){}const requestId=watched?String((init&&init.__cwaRequestId)||window.__CWA_BROKER_REQUEST_ID__||''):null;if(watched&&requestId)window.__CWA_LAST_SUBMIT_REQUEST_ID__=String(requestId);"
              "if(watched){let temporary=false,model=null,thinkingEffort=null,reasoningEffort=null,effort=null,parentMessageId=null;try{const raw=init&&init.body;if(typeof raw==='string'){const payload=JSON.parse(raw);temporary=payload&&payload.history_and_training_disabled===true;model=typeof payload?.model==='string'?payload.model:null;thinkingEffort=typeof payload?.thinking_effort==='string'?payload.thinking_effort:null;reasoningEffort=typeof payload?.reasoning_effort==='string'?payload.reasoning_effort:null;effort=typeof payload?.effort==='string'?payload.effort:null;parentMessageId=typeof payload?.parent_message_id==='string'?payload.parent_message_id:null;}}catch(_){}const expected=String((init&&init.__cwaExpectedProfile)||window.__CWA_EXPECTED_PROFILE__||'').trim().toUpperCase();const profileMatch=!expected||(expected==='HIGH'&&thinkingEffort==='extended')||(expected==='MEDIUM'&&thinkingEffort==='standard')||(expected==='INSTANT'&&!thinkingEffort&&!(model||'').includes('-thinking'));const expectedParent=String((init&&init.__cwaExpectedParentMessageId)||window.__CWA_EXPECTED_PARENT_MESSAGE_ID__||'').trim();const parentMatch=!expectedParent||parentMessageId===expectedParent;const signal=(init&&init.signal)||(input&&input.signal)||null;const signalPresent=!!signal;const signalAborted=!!(signal&&signal.aborted);const keepalive=!!((init&&init.keepalive)||(input&&input.keepalive));const requestMode=String((init&&init.mode)||(input&&input.mode)||'');try{post({phase:'request',endpoint,temporary_mode:temporary,model,thinking_effort:thinkingEffort,reasoning_effort:reasoningEffort,effort,profile_match:profileMatch,expected_profile:expected,parent_message_id:parentMessageId,parent_match:parentMatch,expected_parent_message_id:expectedParent,signal_present:signalPresent,signal_aborted:signalAborted,keepalive,request_mode:requestMode},requestId);}catch(_){}if(expected&&!profileMatch){try{post({phase:'profile_error',model,thinking_effort:thinkingEffort,expected_profile:expected},requestId);}catch(_){}throw new Error('CWA_PROFILE_MISMATCH')}if(expectedParent&&!parentMatch){try{post({phase:'parent_error',parent_message_id:parentMessageId,expected_parent_message_id:expectedParent},requestId);}catch(_){}throw new Error('CWA_PARENT_MISMATCH')}}"
              "try{"
                "const proxyEnabled=watched&&endpoint==='/backend-api/f/conversation'&&((init&&init.__cwaProxyProtectedWrite===true)||window.__CWA_PROXY_PROTECTED_WRITE__===true);"
                "const response=proxyEnabled?await proxyFetch(input,init,requestId):await originalFetch.apply(this,arguments);"
                "if(watched){try{post({phase:'response',status:response.status,proxy_dispatch:proxyEnabled},requestId);}catch(_){}}"
                "return response;"
              "}catch(error){"
                "if(watched){try{post({phase:'error',error:String(error)},requestId);}catch(_){}}"
                "throw error;"
              "}"
            "};"
            "try{wrapped.__cwaIncludesSubmitObserver=true;wrapped.__cwaIncludesStreamObserver=originalFetch.__cwaIncludesStreamObserver===true;}catch(_){}window.fetch=wrapped;window.__cwaSubmitFetchWrapper=wrapped;window.__cwaRestoreSubmitFetchObserver=()=>{if(window.fetch===wrapped)window.fetch=originalFetch;return window.fetch===originalFetch;};return true;};"
            "window.__cwaRearmSubmitFetchObserver=install;return window.__CWA_DEFER_SUBMIT_OBSERVER__===true?true:install();"
            "})()";
}

static NSString *PassiveStreamObservationScript(void) {
    return @"(()=>{"
            "if(window.__cwaWKStreamObservationInstalled&&typeof window.__cwaRearmStreamFetchObserver==='function')return window.__cwaRearmStreamFetchObserver();window.__cwaWKStreamObservationInstalled=true;"
            "const originalWebSocket=window.WebSocket;const originalWorker=window.Worker;const originalSharedWorker=window.SharedWorker;const originalMessageChannel=window.MessageChannel;const originalMessagePort=window.MessagePort;const originalBroadcastChannel=window.BroadcastChannel;const originalXHR=window.XMLHttpRequest;"
            "let transportDiag={fetch:[],xhr:[],worker:[],shared_worker:[],message_port:[],broadcast:[],ws:[],readable:[],ws_frames:0,readable_chunks:0};"
            "const diagPush=(kind,value)=>{const list=transportDiag[kind];if(!Array.isArray(list)||list.length>=24)return;list.push(value);};"
            "const diagShape=(value)=>{if(value==null)return {kind:'null'};if(typeof value==='string')return {kind:'string',length:value.length};if(Array.isArray(value))return {kind:'array',length:value.length};if(typeof value==='object')return {kind:'object',keys:Object.keys(value).slice(0,24),type:typeof value.type==='string'?value.type:null,conversation_id:typeof value.conversation_id==='string'?value.conversation_id:null};return {kind:typeof value};};"
            "const post=(body,requestId=null)=>{try{const id=requestId||currentObserverRequestId||window.__CWA_BROKER_REQUEST_ID__;if(id)body={...body,request_id:id};window.webkit.messageHandlers.cwaStream.postMessage(body)}catch(_){}};"
            "const str=(v)=>typeof v==='string'&&v.trim()?v.trim():null;"
            "const sensitiveKey=(key)=>/(?:^|_)(?:token|secret|authorization|cookie)(?:$|_)/i.test(String(key||''))||String(key||'').toLowerCase()==='download_url';"
            "const sanitize=(value,depth=0)=>{if(value==null||depth>24)return value;if(Array.isArray(value))return value.map(item=>sanitize(item,depth+1));if(typeof value!=='object')return value;const out={};for(const [key,item] of Object.entries(value)){if(sensitiveKey(key))continue;out[key]=sanitize(item,depth+1);}return out;};"
            "let sequence=0,currentMessageId=null,currentRecipient='all',currentText='',currentIsFinalText=false,lastHandoffTopic=null,activeWsTopicId=null,activeConversationId=null,currentObserverRequestId=null,observedFetchRequestIds=new Set(),lifecycleFetchArgsSeen=new WeakSet(),lifecycleSignalsSeen=new WeakSet(),turnStartedAt=0;"
            "const beginTurn=(requestId)=>{currentObserverRequestId=String(requestId||'');sequence=0;currentMessageId=null;currentRecipient='all';currentText='';currentIsFinalText=false;lastHandoffTopic=null;activeWsTopicId=null;activeConversationId=null;observedFetchRequestIds=new Set();lifecycleFetchArgsSeen=new WeakSet();lifecycleSignalsSeen=new WeakSet();turnStartedAt=performance.now();transportDiag={fetch:[],xhr:[],worker:[],shared_worker:[],message_port:[],broadcast:[],ws:[],readable:[],ws_frames:0,readable_chunks:0};};"
            "const endTurn=(requestId)=>{if(currentObserverRequestId===String(requestId||'')){currentObserverRequestId=null;activeWsTopicId=null;}};"
            "window.__cwaWKBeginTurnObserver=beginTurn;window.__cwaWKEndTurnObserver=endTurn;"
            "const contentText=(content)=>{if(!content||typeof content!=='object')return '';if(typeof content.text==='string')return content.text;if(typeof content.content==='string')return content.content;const parts=Array.isArray(content.parts)?content.parts:[];let out='';for(const part of parts.slice(0,64)){if(typeof part==='string')out+=part;else if(part&&typeof part.text==='string')out+=part.text;}return out;};"
            "const emitText=(type,id,value)=>{sequence+=1;const event={phase:'text',type,sequence,message_id:id||null};if(type==='assistant_text_delta')event.delta=value;else event.text=value;post(event);};"
            "const applyText=(text)=>{if(typeof text!=='string'||!currentMessageId||currentRecipient!=='all'||text===currentText)return;if(text.startsWith(currentText)){const delta=text.slice(currentText.length);currentText=text;if(delta)emitText('assistant_text_delta',currentMessageId,delta);}else{currentText=text;emitText('assistant_text_revision',currentMessageId,text);}};"
            "const completedStatus=(value)=>['completed','complete','finished','done','success','succeeded','finished_successfully'].includes(String(value||'').toLowerCase());"
            "const messageTerminal=(message)=>{if(!message||typeof message!=='object')return false;const metadata=message.metadata&&typeof message.metadata==='object'?message.metadata:{};const finishDetails=metadata.finish_details&&typeof metadata.finish_details==='object'?metadata.finish_details:null;return message.end_turn===true||completedStatus(message.status)||completedStatus(message.async_status)||completedStatus(metadata.status)||completedStatus(metadata.async_status)||(finishDetails&&!!str(finishDetails.type))||!!str(metadata.finish_reason)||!!str(message.finish_reason);};"
            "const emitTerminal=()=>post({phase:'terminal',message_id:currentMessageId||null});"
            "const inspectTerminalPatch=(path,value)=>{if(!currentIsFinalText)return;const p=String(path||'');if((p==='/message/end_turn'&&value===true)||(p==='/message/status'&&completedStatus(value))||(p==='/message/async_status'&&completedStatus(value))||(p==='/message/metadata/status'&&completedStatus(value))||(p==='/message/metadata/async_status'&&completedStatus(value))||(p==='/message/metadata/finish_reason'&&!!str(value))||(p==='/message/finish_reason'&&!!str(value))||(p==='/message/metadata/finish_details'&&value&&typeof value==='object'&&!!str(value.type))){emitTerminal();}};"
            "const selectMessage=(message)=>{if(!message||typeof message!=='object')return;const id=str(message.id);const previousMessageId=currentMessageId;const role=str(message.author&&message.author.role)||'';currentRecipient=str(message.recipient)||'all';const contentType=str(message.content&&message.content.content_type)||'';if(id)currentMessageId=id;currentIsFinalText=role==='assistant'&&currentRecipient==='all'&&contentType==='text'&&!(message.metadata&&message.metadata.is_thinking_preamble_message===true);if(!currentIsFinalText)return;const text=contentText(message.content);if(id&&id!==previousMessageId){currentText='';if(text){currentText=text;emitText('assistant_text_snapshot',currentMessageId,text);}}else if(currentText===''&&text){currentText=text;emitText('assistant_text_snapshot',currentMessageId,text);}else applyText(text);if(messageTerminal(message))emitTerminal();};"
            "const inspectIdentity=(value,depth=0,seen=null)=>{if(value==null||depth>8||typeof value!=='object')return;const visited=seen||new Set();if(visited.has(value))return;visited.add(value);if(Array.isArray(value)){for(const item of value.slice(0,128))inspectIdentity(item,depth+1,visited);return;}if(value.author&&value.content)selectMessage(value);const type=str(value.type)||'';const conversation=str(value.conversation_id);const exchange=str(value.turn_exchange_id)||str(value.working_turn_id);if(conversation&&!activeConversationId)activeConversationId=conversation;if(type==='resume_conversation_token'&&str(value.token)){if(conversation)activeConversationId=conversation;post({phase:'resume',token:str(value.token),conversation_id:conversation});}let topic=null;const options=Array.isArray(value.options)?value.options:[];for(const option of options.slice(0,32)){if(option&&option.type==='subscribe_ws_topic'&&str(option.topic_id)){topic=str(option.topic_id);break;}}if(!topic&&exchange)topic='conversation-turn-'+exchange;if(topic){activeWsTopicId=topic;}if(topic&&topic!==lastHandoffTopic){lastHandoffTopic=topic;post({phase:'handoff',topic_id:topic,conversation_id:conversation,turn_exchange_id:exchange});}for(const key of Object.keys(value).slice(0,128)){if(sensitiveKey(key))continue;inspectIdentity(value[key],depth+1,visited);}};"
            "const processPayload=(payload)=>{if(payload&&typeof payload==='object'){const parsed=sanitize(payload);if(parsed&&typeof parsed==='object')post({phase:'raw',parsed});}inspectIdentity(payload);if(!payload||typeof payload!=='object')return;const value=payload.v,path=payload.p;if(value&&typeof value==='object'&&!Array.isArray(value)&&value.message)selectMessage(value.message);if(typeof value==='string'&&currentRecipient==='all'&&(path==null||path==='/message/content/parts/0')){currentText+=value;emitText('assistant_text_delta',currentMessageId,value);}inspectTerminalPatch(path,value);if(Array.isArray(value)){for(const item of value.slice(0,128)){if(!item||typeof item!=='object')continue;if(item.v&&typeof item.v==='object'&&!Array.isArray(item.v)&&item.v.message)selectMessage(item.v.message);if(item.p==='/message/content/parts/0'&&typeof item.v==='string'&&currentRecipient==='all'){currentText+=item.v;emitText('assistant_text_delta',currentMessageId,item.v);}else if(item.p==='/message/content'&&item.v&&typeof item.v==='object'&&currentRecipient==='all'){applyText(contentText(item.v));}inspectTerminalPatch(item.p,item.v);}}};"
            "const isWrite=(url,method)=>{if(String(method||'GET').toUpperCase()!=='POST')return false;try{const u=new URL(url,location.href);const p=u.pathname.replace(/\\/+$/,'');return u.origin===location.origin&&(p.endsWith('/backend-api/conversation')||p.endsWith('/backend-api/f/conversation')||p.endsWith('/backend-api/f/conversation/resume'));}catch(_){return false;}};"
            "const parseEncodedStreamItem=(encoded)=>{if(typeof encoded!=='string'||!encoded)return null;let last=null,current=[];for(const line of encoded.replace(/\\r\\n/g,'\\n').split('\\n')){if(!line){if(current.length)last=current.join('\\n');current=[];continue;}if(line.startsWith('data:'))current.push(line.slice(5).trimStart());}if(current.length)last=current.join('\\n');return typeof last==='string'?last.trim():null;};"
            "const findConversationId=(value,depth=0)=>{if(value==null||depth>7)return null;if(Array.isArray(value)){for(const item of value.slice(0,128)){const found=findConversationId(item,depth+1);if(found)return found;}return null;}if(typeof value!=='object')return null;const direct=str(value.conversation_id);if(direct)return direct;for(const key of ['message','messages','data','result','payload','turn','v','value']){if(Object.prototype.hasOwnProperty.call(value,key)){const found=findConversationId(value[key],depth+1);if(found)return found;}}return null;};"
            "const processWebSocketTopicMessage=(item)=>{if(!currentObserverRequestId||!item||typeof item!=='object')return;const itemTopic=str(item.topic_id);if(!itemTopic)return;const outer=item.payload;if(!outer||typeof outer!=='object'||outer.type!=='conversation-turn-stream')return;const inner=outer.payload;if(!inner||typeof inner!=='object')return;if(inner.type==='done'){if(itemTopic===activeWsTopicId)post({phase:'done'});return;}if(inner.type!=='stream-item')return;const data=parseEncodedStreamItem(inner.encoded_item);if(!data)return;if(data==='[DONE]'){if(itemTopic===activeWsTopicId)post({phase:'done'});return;}let payload;try{payload=JSON.parse(data);}catch(_){return;}if(!activeWsTopicId&&activeConversationId){const candidate=findConversationId(payload);if(candidate===activeConversationId){activeWsTopicId=itemTopic;lastHandoffTopic=itemTopic;post({phase:'handoff',topic_id:itemTopic,conversation_id:candidate});}}if(itemTopic!==activeWsTopicId)return;processPayload(payload);};"
            "const processGlobalCompletion=(item)=>{if(!currentObserverRequestId||!item||typeof item!=='object'||item.type!=='message'||item.topic_id!=='conversations')return;const outer=item.payload;if(!outer||typeof outer!=='object'||outer.type!=='conversation-turn-complete')return;const inner=outer.payload;const conversationId=str(inner&&inner.conversation_id);if(!conversationId)return;post({phase:'global_completion',conversation_id:conversationId});const fallbacks=window.__cwaDOMFallbacks||{};const fallback=fallbacks[currentObserverRequestId];if(fallback&&typeof fallback.complete==='function'){try{fallback.complete(conversationId);}catch(_){}}};"
            "const processWebSocketFrame=(raw)=>{if(!currentObserverRequestId||typeof raw!=='string')return;let parsed;try{parsed=JSON.parse(raw);}catch(_){diagPush('ws',{kind:'unparsed_string',length:raw.length});return;}const items=Array.isArray(parsed)?parsed.slice(0,128):[parsed];for(const item of items){if(!item||typeof item!=='object')continue;const outer=item.payload&&typeof item.payload==='object'?item.payload:null;const inner=outer&&outer.payload&&typeof outer.payload==='object'?outer.payload:null;diagPush('ws',{kind:'item',type:typeof item.type==='string'?item.type:null,topic_id:typeof item.topic_id==='string'?item.topic_id:null,payload_type:outer&&typeof outer.type==='string'?outer.type:null,inner_type:inner&&typeof inner.type==='string'?inner.type:null,inner_keys:inner?Object.keys(inner).slice(0,16):[]});if(item.type==='message'){processGlobalCompletion(item);processWebSocketTopicMessage(item);}const catchups=item&&item.reply&&item.reply.catchups;if(Array.isArray(catchups)){for(const catchup of catchups.slice(0,128)){processGlobalCompletion(catchup);processWebSocketTopicMessage(catchup);}}}};"
            "if(typeof originalWebSocket==='function'){const observedSockets=new WeakSet();const originalSocketAdd=originalWebSocket.prototype&&originalWebSocket.prototype.addEventListener;const observeSocket=(socket)=>{if(!socket||observedSockets.has(socket)||typeof originalSocketAdd!=='function')return;observedSockets.add(socket);try{originalSocketAdd.call(socket,'message',event=>{transportDiag.ws_frames=(transportDiag.ws_frames||0)+1;processWebSocketFrame(event&&event.data);});}catch(_){}};if(typeof originalSocketAdd==='function'){originalWebSocket.prototype.addEventListener=function(type,listener,options){if(type==='message')observeSocket(this);return originalSocketAdd.call(this,type,listener,options);};}let WebSocketProxy=null;WebSocketProxy=new Proxy(originalWebSocket,{construct(target,args,newTarget){const ctor=newTarget===WebSocketProxy?target:newTarget;const socket=Reflect.construct(target,args,ctor);observeSocket(socket);return socket;}});window.WebSocket=WebSocketProxy;}"
            "const observedPorts=new WeakSet();const originalPortAdd=originalMessagePort&&originalMessagePort.prototype&&originalMessagePort.prototype.addEventListener;const observePort=(port,kind='message_port')=>{if(!port||observedPorts.has(port)||typeof originalPortAdd!=='function')return;observedPorts.add(port);try{originalPortAdd.call(port,'message',event=>{diagPush(kind,diagShape(event&&event.data));for(const child of Array.from((event&&event.ports)||[]))observePort(child,'message_port');});}catch(_){}};if(typeof originalPortAdd==='function'){originalMessagePort.prototype.addEventListener=function(type,listener,options){if(type==='message')observePort(this,'message_port');return originalPortAdd.call(this,type,listener,options);};}"
            "if(typeof originalMessageChannel==='function'){let MessageChannelProxy=null;MessageChannelProxy=new Proxy(originalMessageChannel,{construct(target,args,newTarget){const ctor=newTarget===MessageChannelProxy?target:newTarget;const channel=Reflect.construct(target,args,ctor);observePort(channel&&channel.port1,'message_port');observePort(channel&&channel.port2,'message_port');return channel;}});window.MessageChannel=MessageChannelProxy;}"
            "if(typeof originalWorker==='function'){const observedWorkers=new WeakSet();const originalWorkerAdd=originalWorker.prototype&&originalWorker.prototype.addEventListener;const observeWorker=(worker)=>{if(!worker||observedWorkers.has(worker)||typeof originalWorkerAdd!=='function')return;observedWorkers.add(worker);try{originalWorkerAdd.call(worker,'message',event=>{diagPush('worker',diagShape(event&&event.data));for(const port of Array.from((event&&event.ports)||[]))observePort(port,'message_port');});}catch(_){}};if(typeof originalWorkerAdd==='function'){originalWorker.prototype.addEventListener=function(type,listener,options){if(type==='message')observeWorker(this);return originalWorkerAdd.call(this,type,listener,options);};}let WorkerProxy=null;WorkerProxy=new Proxy(originalWorker,{construct(target,args,newTarget){const ctor=newTarget===WorkerProxy?target:newTarget;const worker=Reflect.construct(target,args,ctor);observeWorker(worker);return worker;}});window.Worker=WorkerProxy;}"
            "if(typeof originalSharedWorker==='function'){let SharedWorkerProxy=null;SharedWorkerProxy=new Proxy(originalSharedWorker,{construct(target,args,newTarget){const ctor=newTarget===SharedWorkerProxy?target:newTarget;const worker=Reflect.construct(target,args,ctor);diagPush('shared_worker',{kind:'created'});observePort(worker&&worker.port,'shared_worker');return worker;}});window.SharedWorker=SharedWorkerProxy;}"
            "if(typeof originalBroadcastChannel==='function'){const observedBroadcast=new WeakSet();const originalBroadcastAdd=originalBroadcastChannel.prototype&&originalBroadcastChannel.prototype.addEventListener;const observeBroadcast=(channel)=>{if(!channel||observedBroadcast.has(channel)||typeof originalBroadcastAdd!=='function')return;observedBroadcast.add(channel);try{originalBroadcastAdd.call(channel,'message',event=>diagPush('broadcast',diagShape(event&&event.data)));}catch(_){}};if(typeof originalBroadcastAdd==='function'){originalBroadcastChannel.prototype.addEventListener=function(type,listener,options){if(type==='message')observeBroadcast(this);return originalBroadcastAdd.call(this,type,listener,options);};}let BroadcastProxy=null;BroadcastProxy=new Proxy(originalBroadcastChannel,{construct(target,args,newTarget){const ctor=newTarget===BroadcastProxy?target:newTarget;const channel=Reflect.construct(target,args,ctor);observeBroadcast(channel);return channel;}});window.BroadcastChannel=BroadcastProxy;}"
            "window.__cwaWKTransportDiag=()=>transportDiag;"
            "const processBlock=(block)=>{const data=String(block||'').split(/\\r?\\n/).filter(line=>line.startsWith('data:')).map(line=>line.slice(5).trimStart()).join('\\n').trim();if(!data)return;if(data==='[DONE]'){post({phase:'done'});return;}try{processPayload(JSON.parse(data));}catch(_){}};"
            "const ignoredReadableStreams=new WeakSet(),responseStreamMeta=new WeakMap(),readerMeta=new WeakMap();let readableTapInstalled=false;try{const bodyDescriptor=typeof Response==='function'?Object.getOwnPropertyDescriptor(Response.prototype,'body'):null;const originalGetReader=typeof ReadableStream==='function'&&ReadableStream.prototype?ReadableStream.prototype.getReader:null;const readerProto=typeof ReadableStreamDefaultReader==='function'?ReadableStreamDefaultReader.prototype:null;const originalRead=readerProto&&readerProto.read;if(bodyDescriptor&&typeof bodyDescriptor.get==='function'&&bodyDescriptor.configurable!==false&&typeof originalGetReader==='function'&&typeof originalRead==='function'){Object.defineProperty(Response.prototype,'body',{...bodyDescriptor,get:function(){const body=bodyDescriptor.get.call(this);try{const requestId=currentObserverRequestId||window.__CWA_BROKER_REQUEST_ID__||null;const rawUrl=String(this&&this.url||'');if(body&&requestId&&rawUrl){const u=new URL(rawUrl,location.href);const p=u.pathname.replace(/\\/+$/,'');if(u.origin===location.origin&&(p.endsWith('/backend-api/conversation')||p.endsWith('/backend-api/f/conversation')||p.endsWith('/backend-api/f/conversation/resume'))){const meta={requestId:String(requestId),status:Number(this.status)||0,path:p};responseStreamMeta.set(body,meta);diagPush('readable',{kind:'body',status:meta.status,path:p});}}}catch(_){}return body;}});ReadableStream.prototype.getReader=function(...args){const reader=Reflect.apply(originalGetReader,this,args);try{const meta=responseStreamMeta.get(this);if(meta&&!ignoredReadableStreams.has(this)){readerMeta.set(reader,meta);diagPush('readable',{kind:'reader',status:meta.status,path:meta.path});}}catch(_){}return reader;};readerProto.read=function(...args){const meta=readerMeta.get(this);const pending=Reflect.apply(originalRead,this,args);if(!meta)return pending;return Promise.resolve(pending).then(result=>{try{transportDiag.readable_chunks=(transportDiag.readable_chunks||0)+1;const value=result&&result.value;diagPush('readable',{kind:'chunk',done:!!(result&&result.done),bytes:value&&typeof value.byteLength==='number'?value.byteLength:0,status:meta.status,path:meta.path});}catch(_){}return result;});};readableTapInstalled=true;}}catch(_){}window.__cwaReadableTapInstalled=readableTapInstalled;"
            "const observe=async(response,requestId=null)=>{if(!response)return;const body=response.body;if(!body)return;ignoredReadableStreams.add(body);const scoped=!!requestId;if(scoped&&currentObserverRequestId!==requestId)return;post({phase:'started',status:Number(response.status)||0,ok:response.ok===true},requestId);const reader=body.getReader();const decoder=new TextDecoder();let buffer='';try{while(!scoped||currentObserverRequestId===requestId){const chunk=await reader.read();if(scoped&&currentObserverRequestId!==requestId)break;if(chunk.done){buffer+=decoder.decode();break;}buffer+=decoder.decode(chunk.value,{stream:true});if(buffer.length>1000000)buffer=buffer.slice(-1000000);while(true){const m=/\\r?\\n\\r?\\n/.exec(buffer);if(!m)break;const block=buffer.slice(0,m.index);buffer=buffer.slice(m.index+m[0].length);processBlock(block);}}if(!scoped||currentObserverRequestId===requestId){const tail=buffer.trim();if(tail)processBlock(tail);}}catch(_){}finally{try{reader.cancel()}catch(_){}try{reader.releaseLock()}catch(_){}post({phase:'ended'},requestId);}};"
            "window.__cwaObserveStreamResponse=observe;"

            "const lifecycleMeta=(input,init)=>{let url='',method='GET',path='';try{if(input instanceof Request){url=input.url;method=(init&&init.method)||input.method;}else{url=String(input||'');method=(init&&init.method)||'GET';}const u=new URL(url,location.href);path=u.pathname.replace(/\\/+$/,'');if(u.origin!==location.origin)return null;const relevant=path==='/backend-api/f/conversation'||path==='/backend-api/conversation'||path==='/backend-api/f/conversation/prepare'||path==='/backend-api/sentinel/chat-requirements/prepare'||path==='/backend-api/sentinel/chat-requirements/finalize';if(!relevant)return null;return {url,path,method:String(method||'GET').toUpperCase()};}catch(_){return null;}};"
            "const lifecycleDiagAllowed=(input,init)=>{const target=init&&typeof init==='object'?init:(input&&typeof input==='object'?input:null);if(!target)return true;try{if(lifecycleFetchArgsSeen.has(target))return false;lifecycleFetchArgsSeen.add(target);}catch(_){}return true;};"
            "const installFetch=()=>{const originalFetch=window.fetch;if(typeof originalFetch!=='function')return false;if(originalFetch.__cwaIncludesStreamObserver===true)return true;const wrapped=function(...args){const requestId=currentObserverRequestId||window.__CWA_BROKER_REQUEST_ID__||null;const input=args[0],init=args[1],meta=currentObserverRequestId?lifecycleMeta(input,init):null;const trace=!!meta&&lifecycleDiagAllowed(input,init);const started=performance.now();if(trace)diagPush('fetch',{phase:'request',path:meta.path,method:meta.method,t_ms:Math.round(started-turnStartedAt)});let signal=null;try{signal=(init&&init.signal)||(input instanceof Request?input.signal:null);}catch(_){}if(trace&&meta&&isWrite(meta.url,meta.method)&&signal&&typeof signal==='object'&&!lifecycleSignalsSeen.has(signal)){try{lifecycleSignalsSeen.add(signal);diagPush('fetch',{phase:'signal_state',path:meta.path,t_ms:Math.round(performance.now()-turnStartedAt),aborted:signal.aborted===true});if(typeof signal.addEventListener==='function')signal.addEventListener('abort',()=>{let reasonName='',reasonText='';try{const reason=signal.reason;reasonName=String(reason&&reason.name||'');reasonText=String(reason||'').slice(0,160);}catch(_){}diagPush('fetch',{phase:'signal_abort',path:meta.path,t_ms:Math.round(performance.now()-turnStartedAt),reason_name:reasonName,reason:reasonText});},{once:true});}catch(_){}}let pending;try{pending=Reflect.apply(originalFetch,this,args);}catch(error){if(trace)diagPush('fetch',{phase:'reject',path:meta.path,method:meta.method,t_ms:Math.round(performance.now()-turnStartedAt),duration_ms:Math.round(performance.now()-started),error_name:String(error&&error.name||''),error:String(error||'').slice(0,160)});throw error;}return Promise.resolve(pending).then(response=>{if(trace)diagPush('fetch',{phase:'response',path:meta.path,method:meta.method,status:Number(response&&response.status)||0,t_ms:Math.round(performance.now()-turnStartedAt),duration_ms:Math.round(performance.now()-started)});let url='',method='GET',directObserve=false;try{directObserve=!!(init&&init.__cwaDirectObserve===true);if(input instanceof Request){url=input.url;method=(init&&init.method)||input.method;}else{url=String(input||'');method=(init&&init.method)||'GET';}}catch(_){}if(isWrite(url,method)&&!directObserve){const key=String(requestId||'');if(!key||!observedFetchRequestIds.has(key)){try{const clone=response.clone();if(key)observedFetchRequestIds.add(key);void observe(clone,requestId);}catch(_){}}}return response;},error=>{if(trace)diagPush('fetch',{phase:'reject',path:meta.path,method:meta.method,t_ms:Math.round(performance.now()-turnStartedAt),duration_ms:Math.round(performance.now()-started),error_name:String(error&&error.name||''),error:String(error||'').slice(0,160)});throw error;});};try{wrapped.__cwaIncludesStreamObserver=true;wrapped.__cwaIncludesSubmitObserver=originalFetch.__cwaIncludesSubmitObserver===true;}catch(_){}window.fetch=wrapped;window.__cwaStreamFetchWrapper=wrapped;return true;};"
            "window.__cwaRearmStreamFetchObserver=installFetch;return installFetch();"
            "})()";
}

static NSString *RearmFetchObserversScript(void) {
    return @"(()=>{"
            "let submit=false,stream=false;"
            "try{submit=typeof window.__cwaRearmSubmitFetchObserver==='function'&&window.__cwaRearmSubmitFetchObserver()===true;}catch(_){}"
            "try{stream=typeof window.__cwaRearmStreamFetchObserver==='function'&&window.__cwaRearmStreamFetchObserver()===true;}catch(_){}"
            "const fetch=window.fetch;const submitAttached=!!(fetch&&fetch.__cwaIncludesSubmitObserver===true);const streamAttached=!!(fetch&&fetch.__cwaIncludesStreamObserver===true);"
            "return JSON.stringify({ok:submit&&stream&&submitAttached&&streamAttached,submit,stream,submitAttached,streamAttached});"
            "})()";
}

static NSString *ScrollBottomScript(void) {
    return @"(()=>{"
            "const root=document.scrollingElement||document.documentElement||document.body;"
            "if(root)root.scrollTop=root.scrollHeight;"
            "for(const e of document.querySelectorAll('[class*=overflow],[class*=scroll]')){"
              "try{if(e.scrollHeight>e.clientHeight)e.scrollTop=e.scrollHeight}catch(_){}}"
            "window.scrollTo(0,document.body?document.body.scrollHeight:0);"
            "return true;"
            "})()";
}

static BOOL ModeMatches(NSDictionary *snapshot, NSString *requested);

static NSString *ComposerResolverSource(void) {
    return @"()=>{"
            "const visible=(element)=>{if(!(element instanceof Element))return false;const rect=element.getBoundingClientRect();if(rect.width<=0||rect.height<=0)return false;const style=getComputedStyle(element);return style.display!=='none'&&style.visibility!=='hidden'&&style.opacity!=='0';};"
            "const writable=(element)=>{if(!(element instanceof Element))return false;if(element.getAttribute('aria-disabled')==='true')return false;if(element.disabled===true||element.readOnly===true)return false;if(element.hasAttribute('contenteditable')&&element.getAttribute('contenteditable')!=='true')return false;return true;};"
            "const structural=(element)=>{if(element.closest('[data-testid*=\"composer\"]'))return true;const testId=String(element.getAttribute('data-testid')||'').toLowerCase();if(testId.includes('composer')||testId.includes('prompt'))return true;const form=element.closest('form');if(!form)return false;return form.querySelectorAll('button[type=\"submit\"],button[data-testid*=\"send\"],button[data-testid*=\"submit\"]').length>0;};"
            "const score=(element)=>{let value=0;if(element.id==='prompt-textarea')value+=1000;if(element.getAttribute('data-lexical-editor')==='true')value+=900;if(element.matches('textarea[placeholder]'))value+=800;if(element.getAttribute('contenteditable')==='true')value+=500;if(element.getAttribute('role')==='textbox')value+=120;if(element.getAttribute('aria-multiline')==='true')value+=100;if(element.closest('form'))value+=120;if(element.closest('[data-testid*=\"composer\"]'))value+=120;return value;};"
            "const selectors=['#prompt-textarea','[contenteditable=\"true\"][data-lexical-editor=\"true\"]','textarea[placeholder]','[contenteditable=\"true\"]'];const seen=new Set(),candidates=[];let order=0;"
            "for(const selector of selectors){for(const element of document.querySelectorAll(selector)){if(seen.has(element))continue;seen.add(element);if(!visible(element)||!writable(element))continue;const genericOnly=element.getAttribute('contenteditable')==='true'&&element.id!=='prompt-textarea'&&element.getAttribute('data-lexical-editor')!=='true';if(genericOnly&&!structural(element))continue;candidates.push({element,score:score(element),order});order+=1;}}"
            "candidates.sort((left,right)=>right.score-left.score||right.order-left.order);return candidates.length?candidates[0].element:null;"
            "}";
}

static NSString *ScopedSendResolverSource(void) {
    return @"(composer)=>{"
            "if(!composer)return null;const visible=(button)=>{if(!(button instanceof Element))return false;const rect=button.getBoundingClientRect();if(rect.width<=0||rect.height<=0)return false;const style=getComputedStyle(button);return !button.disabled&&button.getAttribute('aria-disabled')!=='true'&&style.display!=='none'&&style.visibility!=='hidden'&&style.opacity!=='0'&&style.pointerEvents!=='none';};"
            "const scope=composer.closest('form')||composer.closest('[data-testid*=\"composer\"]')||document;"
            "const all=[...scope.querySelectorAll('button[type=\"submit\"],button[data-testid*=\"send\"],button[data-testid*=\"submit\"],button[aria-label]')].filter(visible);"
            "const semantic=all.filter(button=>{const testId=String(button.getAttribute('data-testid')||'').toLowerCase();const aria=String(button.getAttribute('aria-label')||'').toLowerCase();return testId.includes('send')||testId.includes('submit')||aria.includes('send prompt');});"
            "if(semantic.length===1)return semantic[0];const submit=all.filter(button=>button.getAttribute('type')==='submit');return submit.length===1?submit[0]:null;"
            "}";
}

static NSString *ReadinessScript(void) {
    NSString *composerResolver = ComposerResolverSource();
    NSString *sendResolver = ScopedSendResolverSource();
    return [NSString stringWithFormat:
            @"(()=>{"
              "const resolveComposer=%@;const resolveSend=%@;const composer=resolveComposer();"
              "const composerTag=composer?composer.tagName:null;const composerContentEditable=composer?composer.getAttribute('contenteditable'):null;const composerClass=composer?(composer.className||'').toString().slice(0,200):null;const composerTextLength=composer?String(composer.innerText||composer.value||'').length:0;"
              "const send=resolveSend(composer);"
              "const stop=[...document.querySelectorAll('button')].find(b=>{const r=b.getBoundingClientRect();if(!r.width||!r.height)return false;const test=String(b.getAttribute('data-testid')||'');const aria=String(b.getAttribute('aria-label')||'');return test==='stop-button'||test==='stop-generating-button'||/stop answering|stop generating/i.test(aria);})||null;const busy=!!composer&&(composer.getAttribute('aria-busy')==='true'||composer.getAttribute('contenteditable')==='false'||composer.disabled===true);const composerReady=!!composer&&!stop&&!busy;"
              "const body=(document.body&&document.body.innerText)||'';"
              "const messageNodes=[...document.querySelectorAll('[data-message-id]')];"
              "const latestMessageId=messageNodes.length?(messageNodes[messageNodes.length-1].getAttribute('data-message-id')||null):null;"
              "let latestAssistantMessageId=null;for(let i=messageNodes.length-1;i>=0;i--){const node=messageNodes[i];const assistant=node.matches('[data-message-author-role=\"assistant\"]')?node:node.querySelector('[data-message-author-role=\"assistant\"]');if(assistant){latestAssistantMessageId=node.getAttribute('data-message-id')||null;break;}}"
              "let selectedMode=null;const modeCandidates=[];"
              "if(composer){const cr=composer.getBoundingClientRect();for(const e of document.querySelectorAll('button,[role=button],span,div')){if(e.children.length)continue;const t=(e.textContent||'').trim();if(!/^(Instant|Medium|High)$/i.test(t))continue;const r=e.getBoundingClientRect();if(!r.width||!r.height)continue;const d=Math.hypot((r.left+r.width/2)-(cr.left+cr.width/2),(r.top+r.height/2)-(cr.top+cr.height/2));modeCandidates.push({text:t,distance:Math.round(d)});}modeCandidates.sort((a,b)=>a.distance-b.distance);if(modeCandidates.length&&modeCandidates[0].distance<700)selectedMode=modeCandidates[0].text;}"
              "const bodyTail=body.slice(-1200);if(!selectedMode){const lines=bodyTail.split(/\\n+/).map(x=>x.trim()).filter(x=>/^(Instant|Medium|High)$/i.test(x));if(lines.length)selectedMode=lines[lines.length-1];}"
              "return JSON.stringify({url:location.href,title:document.title,composer:!!composer,composerReady,composerFallback:false,composerTag,composerContentEditable,composerClass,composerTextLength,send:!!send,stop:!!stop,latestMessageId,latestAssistantMessageId,selectedMode,modeCandidates:modeCandidates.slice(0,8),login:/log in|sign up/i.test(body.slice(0,4000)),bodyTail});"
            "})()",
            composerResolver,
            sendResolver];
}

static NSString *ComposerDiagnosticsScript(void) {
    NSString *resolver = ComposerResolverSource();
    return [NSString stringWithFormat:
            @"(()=>{"
              "const resolveComposer=%@;const e=resolveComposer();"
              "const all=[...document.querySelectorAll('#prompt-textarea,[contenteditable=\"true\"][data-lexical-editor=\"true\"],textarea[placeholder],[contenteditable=\"true\"]')].map((x,i)=>{const r=x.getBoundingClientRect();const s=getComputedStyle(x);return {i,tag:x.tagName,id:x.id||null,test:x.getAttribute('data-testid')||null,lexical:x.getAttribute('data-lexical-editor')||null,role:x.getAttribute('role')||null,ce:x.getAttribute('contenteditable')||null,visible:r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0',w:Math.round(r.width),h:Math.round(r.height),text_len:String(x.innerText||x.value||'').length,active:x===document.activeElement};});"
              "if(!e)return JSON.stringify({selected:null,candidates:all});"
              "const form=e.closest('form'),scope=e.closest('[data-testid*=\"composer\"]');const r=e.getBoundingClientRect();"
              "return JSON.stringify({selected:{tag:e.tagName,id:e.id||null,test:e.getAttribute('data-testid')||null,lexical:e.getAttribute('data-lexical-editor')||null,role:e.getAttribute('role')||null,ce:e.getAttribute('contenteditable')||null,class:String(e.className||'').slice(0,160),active:e===document.activeElement,connected:e.isConnected,w:Math.round(r.width),h:Math.round(r.height),text_len:String(e.innerText||e.value||'').length,form:!!form,scope_test:scope?scope.getAttribute('data-testid')||null:null},candidates:all});"
            "})()",
            resolver];
}

static NSString *FocusAndSelectComposerScript(void) {
    NSString *resolver = ComposerResolverSource();
    return [NSString stringWithFormat:
            @"(()=>{"
              "const resolveComposer=%@;const e=resolveComposer();"
              "if(!e)return JSON.stringify({ok:false,reason:'no_composer'});"
              "e.focus();"
              "if(e.tagName==='TEXTAREA'){e.setSelectionRange(0,e.value.length);}"
              "else{const r=document.createRange();r.selectNodeContents(e);const s=window.getSelection();s.removeAllRanges();s.addRange(r);}"
              "return JSON.stringify({ok:true,id:e.id||null,test:e.getAttribute('data-testid')||null});"
            "})()",
            resolver];
}

static NSString *ComposerTextScript(void) {
    NSString *resolver = ComposerResolverSource();
    return [NSString stringWithFormat:
            @"(()=>{"
              "const resolveComposer=%@;const e=resolveComposer();"
              "if(!e)return JSON.stringify({ok:false,reason:'no_composer',text:''});"
              "return JSON.stringify({ok:true,text:String(e.innerText||e.value||''),id:e.id||null,test:e.getAttribute('data-testid')||null});"
            "})()",
            resolver];
}

static NSDictionary *NativeFillComposer(WKWebView *webView, NSString *prompt) {
    [webView.window makeFirstResponder:webView];
    NSError *focusError = nil;
    NSDictionary *focused = ParseJSONResult(
        EvaluateSync(webView, FocusAndSelectComposerScript(), 1.0, &focusError)
    );
    if (focusError != nil || ![focused[@"ok"] boolValue]) {
        return @{@"ok": @NO, @"text": @"", @"reason": @"focus_failed"};
    }
    id responder = webView.window.firstResponder;
    if (![responder respondsToSelector:@selector(insertText:replacementRange:)]) {
        responder = webView;
    }
    if ([responder respondsToSelector:@selector(insertText:replacementRange:)]) {
        [(id<NSTextInputClient>)responder
            insertText:(prompt ?: @"")
            replacementRange:NSMakeRange(NSNotFound, 0)];
        NSDictionary *snapshot = ParseJSONResult(
            EvaluateSync(webView, ComposerTextScript(), 1.0, nil)
        );
        NSString *snapshotText = [snapshot[@"text"] isKindOfClass:[NSString class]]
            ? snapshot[@"text"]
            : @"";
        if ([snapshot[@"ok"] boolValue] && snapshotText.length > 0) {
            return snapshot;
        }
    }
    return nil;
}

static NSString *FillScript(NSString *prompt) {
    NSString *literal = JSONStringLiteral(prompt ?: @"");
    NSString *resolver = ComposerResolverSource();
    return [NSString stringWithFormat:
            @"(()=>{"
              "const resolveComposer=%@;const e=resolveComposer();"
              "if(!e)return JSON.stringify({ok:false,reason:'no_composer'});"
              "const text=%@;e.focus();"
              "if(e.tagName==='TEXTAREA'){"
                "const d=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value');if(!d||typeof d.set!=='function')return JSON.stringify({ok:false,reason:'textarea_setter_missing'});d.set.call(e,text);"
                "e.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:text}));"
                "e.dispatchEvent(new Event('change',{bubbles:true}));"
              "}else{"
                "const range=document.createRange();range.selectNodeContents(e);const sel=window.getSelection();sel.removeAllRanges();sel.addRange(range);"
                "document.execCommand('insertText',false,text);"
              "}"
              "return JSON.stringify({ok:true,text:String(e.innerText||e.value||'').slice(0,500),id:e.id||null,test:e.getAttribute('data-testid')||null});"
            "})()",
            resolver,
            literal];
}

static NSString *ClickFileScript(void) {
    return @"(()=>{"
            "const i=document.querySelector('#upload-photos')||document.querySelector('input[type=file][accept*=\"image\"]')||document.querySelector('input[type=file]');"
            "if(!i)return JSON.stringify({ok:false,reason:'no_file_input'});i.click();"
            "return JSON.stringify({ok:true,id:i.id||null,accept:i.accept||null});"
            "})()";
}

static NSString *AttachmentMimeType(NSString *path) {
    NSString *ext = path.pathExtension.lowercaseString;
    if ([ext isEqualToString:@"png"]) return @"image/png";
    if ([ext isEqualToString:@"jpg"] || [ext isEqualToString:@"jpeg"]) return @"image/jpeg";
    if ([ext isEqualToString:@"gif"]) return @"image/gif";
    if ([ext isEqualToString:@"webp"]) return @"image/webp";
    if ([ext isEqualToString:@"heic"] || [ext isEqualToString:@"heif"]) return @"image/heic";
    if ([ext isEqualToString:@"pdf"]) return @"application/pdf";
    if ([ext isEqualToString:@"txt"] || [ext isEqualToString:@"md"]) return @"text/plain";
    if ([ext isEqualToString:@"json"]) return @"application/json";
    return @"application/octet-stream";
}

static NSString *InjectAttachmentFilesScript(NSArray<NSString *> *paths) {
    NSMutableArray *files = [NSMutableArray array];
    for (NSString *path in paths ?: @[]) {
        NSData *data = [NSData dataWithContentsOfFile:path];
        if (!data) return nil;
        [files addObject:@{
            @"name":path.lastPathComponent ?: @"attachment",
            @"type":AttachmentMimeType(path),
            @"base64":[data base64EncodedStringWithOptions:0] ?: @""
        }];
    }
    NSData *jsonData = [NSJSONSerialization dataWithJSONObject:files options:0 error:nil];
    NSString *json = [[NSString alloc] initWithData:jsonData encoding:NSUTF8StringEncoding];
    if (json.length == 0) return nil;
    return [NSString stringWithFormat:
            @"(()=>{const specs=%@;const i=document.querySelector('#upload-photos')||document.querySelector('input[type=file][accept*=\"image\"]')||document.querySelector('input[type=file]');"
              "if(!i)return JSON.stringify({ok:false,reason:'no_file_input'});if(typeof DataTransfer!=='function'||typeof File!=='function')return JSON.stringify({ok:false,reason:'file_api_unavailable'});"
              "const dt=new DataTransfer();for(const s of specs){const raw=atob(s.base64||'');const bytes=new Uint8Array(raw.length);for(let n=0;n<raw.length;n++)bytes[n]=raw.charCodeAt(n);dt.items.add(new File([bytes],s.name||'attachment',{type:s.type||'application/octet-stream'}));}"
              "try{i.files=dt.files}catch(_){const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'files')?.set;if(!setter)return JSON.stringify({ok:false,reason:'files_setter_unavailable'});setter.call(i,dt.files);}"
              "const assignedCount=i.files?i.files.length:0;i.dispatchEvent(new Event('input',{bubbles:true}));i.dispatchEvent(new Event('change',{bubbles:true}));const afterCount=i.files?i.files.length:0;return JSON.stringify({ok:assignedCount===specs.length,count:assignedCount,afterCount});})()",
            json];
}

static NSString *SendButtonPointScript(void) {
    NSString *composerResolver = ComposerResolverSource();
    NSString *sendResolver = ScopedSendResolverSource();
    return [NSString stringWithFormat:
            @"(()=>{"
              "const resolveComposer=%@;const resolveSend=%@;const composer=resolveComposer();const b=resolveSend(composer);"
              "if(!composer)return JSON.stringify({ok:false,reason:'no_composer'});"
              "if(!b)return JSON.stringify({ok:false,reason:'no_send'});"
              "const r=b.getBoundingClientRect();const x=r.left+r.width/2,y=r.top+r.height/2;"
              "const inside=Number.isFinite(x)&&Number.isFinite(y)&&x>=0&&y>=0&&x<=innerWidth&&y<=innerHeight;"
              "if(!inside)return JSON.stringify({ok:false,reason:'send_outside_viewport',x,y,w:r.width,h:r.height,viewportWidth:innerWidth,viewportHeight:innerHeight});"
              "return JSON.stringify({ok:true,x,y,w:r.width,h:r.height,viewportWidth:innerWidth,viewportHeight:innerHeight,aria:b.getAttribute('aria-label'),test:b.getAttribute('data-testid')});"
            "})()",
            composerResolver,
            sendResolver];
}

static NSDictionary *NativeClickSendButton(WKWebView *webView) {
    NSError *pointError = nil;
    NSDictionary *point = ParseJSONResult(
        EvaluateSync(webView, SendButtonPointScript(), 1.0, &pointError)
    );
    if (pointError != nil || ![point[@"ok"] boolValue]) {
        return @{
            @"ok": @NO,
            @"reason": pointError.localizedDescription ?: [point[@"reason"] description] ?: @"send_point_failed"
        };
    }
    double viewportWidth = [point[@"viewportWidth"] doubleValue];
    double viewportHeight = [point[@"viewportHeight"] doubleValue];
    double cssX = [point[@"x"] doubleValue];
    double cssY = [point[@"y"] doubleValue];
    if (
        viewportWidth <= 0
        || viewportHeight <= 0
        || !isfinite(cssX)
        || !isfinite(cssY)
    ) {
        return @{@"ok": @NO, @"reason": @"send_point_invalid"};
    }
    NSRect bounds = webView.bounds;
    double scaleX = bounds.size.width / viewportWidth;
    double scaleY = bounds.size.height / viewportHeight;
    NSPoint localPoint = NSMakePoint(
        cssX * scaleX,
        cssY * scaleY
    );
    NSPoint windowPoint = [webView convertPoint:localPoint toView:nil];
    NSWindow *window = webView.window;
    if (window == nil) {
        return @{@"ok": @NO, @"reason": @"send_window_missing"};
    }
    [window makeFirstResponder:webView];
    NSTimeInterval now = [NSProcessInfo processInfo].systemUptime;
    NSEvent *moved = [NSEvent
        mouseEventWithType:NSEventTypeMouseMoved
        location:windowPoint
        modifierFlags:0
        timestamp:now
        windowNumber:window.windowNumber
        context:nil
        eventNumber:0
        clickCount:0
        pressure:0.0];
    NSEvent *down = [NSEvent
        mouseEventWithType:NSEventTypeLeftMouseDown
        location:windowPoint
        modifierFlags:0
        timestamp:now
        windowNumber:window.windowNumber
        context:nil
        eventNumber:1
        clickCount:1
        pressure:1.0];
    NSEvent *up = [NSEvent
        mouseEventWithType:NSEventTypeLeftMouseUp
        location:windowPoint
        modifierFlags:0
        timestamp:now
        windowNumber:window.windowNumber
        context:nil
        eventNumber:2
        clickCount:1
        pressure:0.0];
    [webView mouseMoved:moved];
    [webView mouseDown:down];
    [webView mouseUp:up];
    return @{
        @"ok": @YES,
        @"strategy": @"native_send_button_click",
        @"aria": [point[@"aria"] description] ?: @"",
        @"test": [point[@"test"] description] ?: @""
    };
}

static NSString *AcceptanceScript(NSString *prompt, NSInteger baselineAssistantCount) {
    NSString *literal = JSONStringLiteral(prompt ?: @"");
    return [NSString stringWithFormat:
            @"(()=>{"
              "const prompt=%@;"
              "const users=[...document.querySelectorAll('[data-message-author-role=\"user\"]')];"
              "const assistants=[...document.querySelectorAll('[data-message-author-role=\"assistant\"]')];"
              "const userSeen=users.some(e=>(e.innerText||'').includes(prompt));"
              "const stop=!!(document.querySelector('button[data-testid=\"stop-button\"]')||[...document.querySelectorAll('button')].find(b=>/stop answering/i.test(b.getAttribute('aria-label')||'')));"
              "const body=(document.body&&document.body.innerText)||'';"
              "const error=/something went wrong|there was an error generating|unusual activity|verify you are human|just a moment/i.test(body.slice(-4000));"
              "return JSON.stringify({url:location.href,userSeen,assistantCount:assistants.length,newAssistant:assistants.length>%ld,stop,error,bodyTail:body.slice(-1200)});"
            "})()", literal, (long)baselineAssistantCount];
}

static NSString *AssistantCountScript(void) {
    return @"document.querySelectorAll('[data-message-author-role=\"assistant\"]').length";
}

static NSString *DOMStreamSnapshotScript(void) {
    return @"(()=>{"
            "const wrappers=[...document.querySelectorAll('[data-message-id]')];"
            "let holder=null;let assistant=null;"
            "for(let i=wrappers.length-1;i>=0;i--){const w=wrappers[i];const a=w.matches('[data-message-author-role=\"assistant\"]')?w:w.querySelector('[data-message-author-role=\"assistant\"]');if(a){holder=w;assistant=a;break;}}"
            "if(!assistant){const assistants=[...document.querySelectorAll('[data-message-author-role=\"assistant\"]')];assistant=assistants.length?assistants[assistants.length-1]:null;holder=assistant?assistant.closest('[data-message-id]'):null;}"
            "const messageId=holder?(holder.getAttribute('data-message-id')||null):null;"
            "const text=assistant?((assistant.innerText||assistant.textContent||'')):'';"
            "const stop=!!(document.querySelector('button[data-testid=\"stop-button\"]')||[...document.querySelectorAll('button')].find(b=>/stop answering/i.test(b.getAttribute('aria-label')||'')));"
            "const assistantCount=document.querySelectorAll('[data-message-author-role=\"assistant\"]').length;"
            "return JSON.stringify({url:location.href,messageId,text,stop,assistantCount});"
            "})()";
}

static NSString *InstallDOMFallbackScript(
    NSString *requestId,
    NSInteger baselineAssistantCount,
    NSString *baselineAssistantMessageId,
    NSString *baselineAssistantText
) {
    NSString *requestLiteral = JSONStringLiteral(requestId ?: @"");
    NSString *baselineMessageLiteral = JSONStringLiteral(
        baselineAssistantMessageId ?: @""
    );
    NSString *baselineTextLiteral = JSONStringLiteral(
        baselineAssistantText ?: @""
    );
    return [NSString stringWithFormat:
        @"(()=>{"
          "const rid=%@,baseline=%ld,baselineMessageId=%@,baselineText=%@;"
          "window.__cwaDOMFallbacks=window.__cwaDOMFallbacks||{};"
          "const prior=window.__cwaDOMFallbacks[rid];if(prior&&typeof prior.disconnect==='function')prior.disconnect();"
          "let disposed=false,scheduled=false,sawStop=false,lastText='',lastMessageId=null,terminalTimer=null,completionConversationId=null;"
          "const post=(body)=>{try{window.webkit.messageHandlers.cwaStream.postMessage({...body,request_id:rid})}catch(_){}};"
          "const snapshot=()=>{"
            "const wrappers=[...document.querySelectorAll('[data-message-id]')];let holder=null,assistant=null;"
            "for(let i=wrappers.length-1;i>=0;i--){const w=wrappers[i];const a=w.matches('[data-message-author-role=\"assistant\"]')?w:w.querySelector('[data-message-author-role=\"assistant\"]');if(a){holder=w;assistant=a;break;}}"
            "if(!assistant){const assistants=[...document.querySelectorAll('[data-message-author-role=\"assistant\"]')];assistant=assistants.length?assistants[assistants.length-1]:null;holder=assistant?assistant.closest('[data-message-id]'):null;}"
            "const messageId=holder?(holder.getAttribute('data-message-id')||null):null;"
            "const text=assistant?((assistant.innerText||assistant.textContent||'')):'';"
            "const stop=!!(document.querySelector('button[data-testid=\"stop-button\"]')||[...document.querySelectorAll('button')].find(b=>/stop answering/i.test(b.getAttribute('aria-label')||'')));"
            "const assistantCount=document.querySelectorAll('[data-message-author-role=\"assistant\"]').length;"
            "let conversationId=null;try{const parts=String(location.pathname||'').split('/');if(parts.length>2&&parts[1]==='c'&&parts[2])conversationId=parts[2];}catch(_){}"
            "return {messageId,text,stop,assistantCount,conversationId};"
          "};"
          "const disconnect=()=>{if(disposed)return;disposed=true;if(terminalTimer){clearTimeout(terminalTimer);terminalTimer=null;}try{observer.disconnect()}catch(_){}delete window.__cwaDOMFallbacks[rid];};"
          "const isNewAssistant=(s)=>!!(s&&s.messageId&&s.text&&(s.messageId!==baselineMessageId||s.assistantCount>baseline||s.text!==baselineText));"
          "const evaluate=()=>{"
            "if(disposed)return;const s=snapshot();if(s.stop)sawStop=true;"
            "if(!isNewAssistant(s))return;"
            "const changed=s.messageId!==lastMessageId||s.text!==lastText;"
            "if(changed){lastMessageId=s.messageId;lastText=s.text;post({phase:'dom_text',message_id:s.messageId,text:s.text,assistant_count:s.assistantCount,stop:s.stop,conversation_id:s.conversationId});if(terminalTimer){clearTimeout(terminalTimer);terminalTimer=null;}}"
            "if(s.stop){if(terminalTimer){clearTimeout(terminalTimer);terminalTimer=null;}return;}"
            "return;"
          "};"
          "const complete=(conversationId)=>{if(disposed||String(window.__CWA_LAST_SUBMIT_REQUEST_ID__||'')!==rid)return false;completionConversationId=String(conversationId||'');evaluate();return completionConversationId.length>0;};"
          "const schedule=()=>{if(disposed||scheduled)return;scheduled=true;const run=()=>{scheduled=false;evaluate();};if(typeof queueMicrotask==='function')queueMicrotask(run);else Promise.resolve().then(run);};"
          "const observer=new MutationObserver(schedule);"
          "observer.observe(document.documentElement||document.body,{subtree:true,childList:true,characterData:true,attributes:true,attributeFilter:['data-message-id','data-message-author-role','aria-label','data-testid','disabled']});"
          "const diagnostics=()=>({disposed,sawStop,lastMessageId:lastMessageId||'',lastTextLength:String(lastText||'').length,completionConversationIdPresent:!!completionConversationId,brokerRequestId:String(window.__CWA_BROKER_REQUEST_ID__||''),lastSubmitRequestId:String(window.__CWA_LAST_SUBMIT_REQUEST_ID__||'')});window.__cwaDOMFallbacks[rid]={disconnect,complete,diagnostics};evaluate();return true;"
        "})()",
        requestLiteral,
        (long)baselineAssistantCount,
        baselineMessageLiteral,
        baselineTextLiteral
    ];
}

static NSString *RemoveDOMFallbackScript(NSString *requestId) {
    NSString *requestLiteral = JSONStringLiteral(requestId ?: @"");
    return [NSString stringWithFormat:
        @"(()=>{const rid=%@,all=window.__cwaDOMFallbacks||{};const item=all[rid];if(item&&typeof item.disconnect==='function')item.disconnect();return true;})()",
        requestLiteral
    ];
}

static NSString *DOMFallbackDiagnosticsScript(NSString *requestId) {
    NSString *requestLiteral = JSONStringLiteral(requestId ?: @"");
    return [NSString stringWithFormat:
        @"(()=>{const rid=%@,all=window.__cwaDOMFallbacks||{},item=all[rid];if(!item)return JSON.stringify({present:false,brokerRequestId:String(window.__CWA_BROKER_REQUEST_ID__||''),lastSubmitRequestId:String(window.__CWA_LAST_SUBMIT_REQUEST_ID__||'')});if(typeof item.diagnostics!=='function')return JSON.stringify({present:true,diagnostics:false});try{return JSON.stringify({present:true,diagnostics:true,...item.diagnostics()});}catch(error){return JSON.stringify({present:true,diagnostics:true,error:String(error)});}})()",
        requestLiteral
    ];
}

static NSString *TransportDiagnosticsScript(void) {
    return @"(()=>{"
            "const all=(performance.getEntriesByType('resource')||[]);const resources=all.map(e=>{let path='';try{const u=new URL(e.name,location.href);path=(u.origin===location.origin?'':u.origin)+u.pathname;}catch(_){path=String(e.name||'').slice(0,240);}return {path,initiator:String(e.initiatorType||''),duration:Math.round(Number(e.duration)||0),transfer:Math.round(Number(e.transferSize)||0)};}).filter(e=>/conversation|stream|backend-api/i.test(e.path)).slice(-16);"
            "let serviceWorker=null;try{const c=navigator.serviceWorker&&navigator.serviceWorker.controller;serviceWorker=c&&c.scriptURL?new URL(c.scriptURL,location.href).pathname:null;}catch(_){}"
            "const resourceSummary=resources.slice(-8).map(e=>String(e.path||'')+'|'+String(e.initiator||'')+'|'+String(e.duration||0)).join(',');"
            "let passiveDiag=null;try{passiveDiag=typeof window.__cwaWKTransportDiag==='function'?window.__cwaWKTransportDiag():null;}catch(_){}const last=(v)=>Array.isArray(v)&&v.length?v[v.length-1]:null;const clip=(v)=>Array.isArray(v)?v.slice(0,24):[];const passiveSummary=passiveDiag?JSON.stringify({fetch_count:Array.isArray(passiveDiag.fetch)?passiveDiag.fetch.length:0,fetch:clip(passiveDiag.fetch),worker_count:Array.isArray(passiveDiag.worker)?passiveDiag.worker.length:0,worker:clip(passiveDiag.worker),worker_last:last(passiveDiag.worker),shared_worker_count:Array.isArray(passiveDiag.shared_worker)?passiveDiag.shared_worker.length:0,shared_worker:clip(passiveDiag.shared_worker),shared_worker_last:last(passiveDiag.shared_worker),message_port_count:Array.isArray(passiveDiag.message_port)?passiveDiag.message_port.length:0,message_port:clip(passiveDiag.message_port),message_port_last:last(passiveDiag.message_port),broadcast_count:Array.isArray(passiveDiag.broadcast)?passiveDiag.broadcast.length:0,broadcast:clip(passiveDiag.broadcast),broadcast_last:last(passiveDiag.broadcast),ws_count:Array.isArray(passiveDiag.ws)?passiveDiag.ws.length:0,ws:clip(passiveDiag.ws),ws_frames:Number(passiveDiag.ws_frames)||0,readable_count:Array.isArray(passiveDiag.readable)?passiveDiag.readable.length:0,readable:clip(passiveDiag.readable),readable_chunks:Number(passiveDiag.readable_chunks)||0}):'';"
            "return JSON.stringify({path:String(location.pathname||''),resources,resource_summary:resourceSummary,passive_summary:passiveSummary,readable_tap_installed:window.__cwaReadableTapInstalled===true,service_worker:serviceWorker,shared_worker_available:typeof SharedWorker==='function',worker_available:typeof Worker==='function',websocket_available:typeof WebSocket==='function',submit_observer_installed:window.__cwaSubmitObserverInstalled===true,stream_observer_installed:window.__cwaWKStreamObservationInstalled===true,fetch_is_submit_wrapper:!!(window.fetch&&window.fetch.__cwaIncludesSubmitObserver===true),fetch_is_stream_wrapper:!!(window.fetch&&window.fetch.__cwaIncludesStreamObserver===true)});"
            "})()";
}

static NSString *StopScript(void) {
    return @"(()=>{"
            "const b=document.querySelector('button[data-testid=\"stop-button\"]')||[...document.querySelectorAll('button')].find(x=>/stop answering/i.test(x.getAttribute('aria-label')||''));"
            "if(!b)return JSON.stringify({ok:false,reason:'no_stop'});b.click();"
            "return JSON.stringify({ok:true,aria:b.getAttribute('aria-label'),test:b.getAttribute('data-testid')});"
            "})()";
}

static NSString *ResumeConversationScript(NSString *conversationId, NSString *resumeValue, NSInteger offset) {
    NSString *conversationLiteral = JSONStringLiteral(conversationId ?: @"");
    NSString *resumeLiteral = JSONStringLiteral(resumeValue ?: @"");
    return [NSString stringWithFormat:
            @"(async()=>{try{"
             "const s=await fetch('/api/auth/session',{credentials:'include',cache:'no-store'});"
             "if(!s.ok)throw new Error('AUTH_SESSION_HTTP_'+s.status);"
             "const j=await s.json();const access=j&&j.accessToken;if(!access)throw new Error('AUTH_SESSION_ACCESS_TOKEN_MISSING');"
             "const r=await fetch('/backend-api/f/conversation/resume',{method:'POST',credentials:'include',cache:'no-store',headers:{Accept:'text/event-stream','Content-Type':'application/json',Authorization:'Bearer '+access,'x-conduit-token':%@,'X-OpenAI-Target-Path':'/backend-api/f/conversation/resume','X-OpenAI-Target-Route':'/backend-api/f/conversation/resume'},body:JSON.stringify({conversation_id:%@,offset:%ld})});"
             "window.webkit.messageHandlers.cwaCanonical.postMessage({ok:r.ok,status:r.status,contentType:r.headers.get('content-type')||''});"
             "}catch(e){window.webkit.messageHandlers.cwaCanonical.postMessage({ok:false,status:0,error:String(e)});}return null;})()",
            resumeLiteral,
            conversationLiteral,
            (long)offset];
}

static NSString *AuthenticatedFetchScript(NSString *endpoint) {
    NSString *literal = JSONStringLiteral(endpoint ?: @"");
    return [NSString stringWithFormat:
            @"(()=>{"
              "const url=%@,cacheKey='__cwaAuthorityAccessToken';"
              "const token=async(force=false)=>{if(force)delete window[cacheKey];const cached=window[cacheKey];if(typeof cached==='string'&&cached)return cached;const s=await fetch('/api/auth/session',{credentials:'include',cache:'no-store'});const session=await s.json();const value=session&&session.accessToken;if(!value)throw new Error('AUTH_SESSION_ACCESS_TOKEN_MISSING');window[cacheKey]=value;return value;};"
              "const request=async()=>{let access=await token();let r=await fetch(url,{credentials:'include',cache:'no-store',headers:{Authorization:'Bearer '+access}});if(r.status===401||r.status===403){access=await token(true);r=await fetch(url,{credentials:'include',cache:'no-store',headers:{Authorization:'Bearer '+access}});}return r;};"
              "request()"
                ".then(async r=>{const body=await r.text();window.webkit.messageHandlers.cwaCanonical.postMessage({ok:r.ok,status:r.status,contentType:r.headers.get('content-type')||'',body});})"
                ".catch(e=>window.webkit.messageHandlers.cwaCanonical.postMessage({ok:false,status:0,contentType:'',error:String(e)}));"
              "return true;"
            "})()", literal];
}

static NSString *CanonicalFetchScript(NSString *conversationId) {
    NSString *escaped = [conversationId stringByAddingPercentEncodingWithAllowedCharacters:[NSCharacterSet URLPathAllowedCharacterSet]] ?: @"";
    NSString *endpoint = [@"/backend-api/conversation/" stringByAppendingString:escaped];
    return AuthenticatedFetchScript(endpoint);
}

static NSString *CanonicalCompletionCheckScript(NSString *conversationId) {
    NSString *idLiteral = JSONStringLiteral(conversationId ?: @"");
    return [NSString stringWithFormat:
            @"(()=>{"
              "const id=%@,url='/backend-api/conversation/'+encodeURIComponent(id),cacheKey='__cwaAuthorityAccessToken';"
              "const completed=v=>['completed','complete','finished','done','success','succeeded','finished_successfully'].includes(String(v||'').toLowerCase());"
              "const token=async(force=false)=>{if(force)delete window[cacheKey];const cached=window[cacheKey];if(typeof cached==='string'&&cached)return cached;const s=await fetch('/api/auth/session',{credentials:'include',cache:'no-store'});const session=await s.json();const value=session&&session.accessToken;if(!value)throw new Error('AUTH_SESSION_ACCESS_TOKEN_MISSING');window[cacheKey]=value;return value;};"
              "const request=async()=>{let access=await token();let r=await fetch(url,{credentials:'include',cache:'no-store',headers:{Authorization:'Bearer '+access}});if(r.status===401||r.status===403){access=await token(true);r=await fetch(url,{credentials:'include',cache:'no-store',headers:{Authorization:'Bearer '+access}});}return r;};"
              "request()"
                ".then(async r=>{"
                  "if(!r.ok){window.webkit.messageHandlers.cwaCanonical.postMessage({ok:false,status:r.status,completed:false});return;}"
                  "const d=await r.json();const mapping=d&&d.mapping&&typeof d.mapping==='object'?d.mapping:{};const current=typeof d.current_node==='string'?d.current_node:'';const node=mapping[current];const m=node&&node.message;const md=m&&m.metadata&&typeof m.metadata==='object'?m.metadata:{};const fd=md&&md.finish_details&&typeof md.finish_details==='object'?md.finish_details:null;const role=m&&m.author&&typeof m.author.role==='string'?m.author.role:null;const recipient=m&&typeof m.recipient==='string'?m.recipient:null;const asyncStatus=(d&&typeof d.async_status==='string'?d.async_status:null)||(d&&typeof d.status==='string'?d.status:null)||(node&&typeof node.async_status==='string'?node.async_status:null)||(node&&typeof node.status==='string'?node.status:null)||(typeof md.async_status==='string'?md.async_status:null)||(typeof md.status==='string'?md.status:null);const messageStatus=m&&typeof m.status==='string'?m.status:null;const active=v=>['running','in_progress','pending','queued','started','streaming'].includes(String(v||'').toLowerCase());"
                  "const finalAssistant=role==='assistant'&&(recipient===null||recipient==='all');const finishType=fd&&typeof fd.type==='string'?fd.type:null;const finishReason=(fd&&typeof fd.reason==='string'?fd.reason:null)||(typeof md.finish_reason==='string'?md.finish_reason:null)||(m&&typeof m.finish_reason==='string'?m.finish_reason:null);const finish=finishType||finishReason;const done=!!(finalAssistant&&!active(asyncStatus)&&!active(messageStatus)&&(finish||completed(asyncStatus)||completed(messageStatus)||(m&&m.end_turn===true)));"
                  "window.webkit.messageHandlers.cwaCanonical.postMessage({ok:true,status:r.status,completed:done,currentNode:current,finishType,finishReason,body:done?JSON.stringify(d):''});"
                "})"
                ".catch(e=>window.webkit.messageHandlers.cwaCanonical.postMessage({ok:false,status:0,completed:false,error:String(e)}));"
              "return true;"
            "})()", idLiteral];
}

static NSString *CanonicalCommitCheckScript(NSString *conversationId, NSString *prompt, NSString *baselineCurrentNode) {
    NSString *idLiteral = JSONStringLiteral(conversationId ?: @"");
    NSString *promptLiteral = JSONStringLiteral(prompt ?: @"");
    NSString *baselineLiteral = JSONStringLiteral(baselineCurrentNode ?: @"");
    return [NSString stringWithFormat:
            @"(()=>{"
              "const id=%@,expectedPrompt=%@,baseline=%@,cacheKey='__cwaAuthorityAccessToken';"
              "const url='/backend-api/conversation/'+encodeURIComponent(id);"
              "const token=async(force=false)=>{if(force)delete window[cacheKey];const cached=window[cacheKey];if(typeof cached==='string'&&cached)return cached;const s=await fetch('/api/auth/session',{credentials:'include',cache:'no-store'});const session=await s.json();const value=session&&session.accessToken;if(!value)throw new Error('AUTH_SESSION_ACCESS_TOKEN_MISSING');window[cacheKey]=value;return value;};"
              "const request=async()=>{let access=await token();let r=await fetch(url,{credentials:'include',cache:'no-store',headers:{Authorization:'Bearer '+access}});if(r.status===401||r.status===403){access=await token(true);r=await fetch(url,{credentials:'include',cache:'no-store',headers:{Authorization:'Bearer '+access}});}return r;};"
              "request()"
                ".then(async r=>{"
                  "if(!r.ok){window.webkit.messageHandlers.cwaCanonical.postMessage({ok:false,status:r.status,committed:false});return;}"
                  "const d=await r.json();const current=typeof d.current_node==='string'?d.current_node:'';"
                  "const mapping=d&&typeof d.mapping==='object'&&d.mapping?d.mapping:{};"
                  "let nodeId=current,found=false;const seen=new Set();"
                  "while(nodeId&&!seen.has(nodeId)){seen.add(nodeId);const node=mapping[nodeId];if(!node||typeof node!=='object')break;const m=node.message;"
                    "if(m&&m.author&&m.author.role==='user'&&m.content&&Array.isArray(m.content.parts)){const rendered=m.content.parts.filter(x=>typeof x==='string').join('\\n');if(rendered.trim()===expectedPrompt.trim()){found=true;break;}}"
                    "nodeId=typeof node.parent==='string'?node.parent:'';"
                  "}"
                  "const changed=!baseline||(current&&current!==baseline);"
                  "window.webkit.messageHandlers.cwaCanonical.postMessage({ok:true,status:r.status,committed:!!(changed&&found),currentNode:current});"
                "})"
                ".catch(e=>window.webkit.messageHandlers.cwaCanonical.postMessage({ok:false,status:0,committed:false,error:String(e)}));"
              "return true;"
            "})()", idLiteral, promptLiteral, baselineLiteral];
}

static NSString *MinimalSecurityShellHTML(void) {
    return @"<!doctype html><meta charset='utf-8'><base href='https://chatgpt.com/'><title>cwa security shell</title>"
            "<script>window.__cwaSentinelLoaded=false</script>"
            "<script src='/backend-api/sentinel/sdk.js' onload='window.__cwaSentinelLoaded=true'></script>"
            "<body></body>";
}

static NSString *MinimalSecurityShellSource(void) {
    NSURL *resourceURL = [[NSBundle mainBundle] URLForResource:@"minimal_security_shell" withExtension:@"js"];
    if (resourceURL == nil) return nil;
    NSError *error = nil;
    NSString *source = [NSString stringWithContentsOfURL:resourceURL encoding:NSUTF8StringEncoding error:&error];
    return source.length > 0 && error == nil ? source : nil;
}

static NSString *MinimalSecurityWriteScript(
    NSString *prompt,
    NSString *profile,
    NSString *conversationId,
    NSString *parentMessageId,
    NSString *selectedModelSlug,
    NSString *selectedThinkingEffort,
    NSString *attachmentsBase64,
    NSString *handoffAttemptId,
    BOOL temporary
) {
    NSString *source = MinimalSecurityShellSource();
    if (source.length == 0) return nil;
    NSString *promptLiteral = JSONStringLiteral(prompt ?: @"");
    NSString *profileLiteral = JSONStringLiteral(profile ?: @"");
    NSString *conversationLiteral = JSONStringLiteral(conversationId ?: @"");
    NSString *parentLiteral = JSONStringLiteral(parentMessageId ?: @"");
    NSString *modelLiteral = JSONStringLiteral(selectedModelSlug ?: @"");
    NSString *effortLiteral = JSONStringLiteral(selectedThinkingEffort ?: @"");
    NSString *attachmentsLiteral = JSONStringLiteral(attachmentsBase64 ?: @"");
    NSString *handoffAttemptLiteral = JSONStringLiteral(handoffAttemptId ?: @"");
    NSString *temporaryLiteral = temporary ? @"true" : @"false";
    return [NSString stringWithFormat:
        @"window.__CWA_MINIMAL_PROMPT__=%@;window.__CWA_MINIMAL_PROFILE__=%@;window.__CWA_MINIMAL_TEMPORARY__=%@;window.__CWA_MINIMAL_CONVERSATION_ID__=%@;window.__CWA_MINIMAL_PARENT_MESSAGE_ID__=%@;window.__CWA_MINIMAL_SELECTED_MODEL_SLUG__=%@;window.__CWA_MINIMAL_SELECTED_THINKING_EFFORT__=%@;window.__CWA_MINIMAL_ATTACHMENTS_BASE64__=%@;window.__CWA_MINIMAL_HANDOFF_ATTEMPT_ID__=%@;\n%@",
        promptLiteral,
        profileLiteral,
        temporaryLiteral,
        conversationLiteral,
        parentLiteral,
        modelLiteral,
        effortLiteral,
        attachmentsLiteral,
        handoffAttemptLiteral,
        source
    ];
}

@interface WKTurnBrokerEntry : NSObject
@property(nonatomic, copy) NSString *requestId;
@property(nonatomic, strong) NSDictionary *request;
@property(nonatomic, strong) WKAuthorityDelegate *delegate;
@property(nonatomic, strong) WKWebView *webView;
@property(nonatomic, strong) WKAuthorityDelegate *pageNavigationDelegate;
@property(nonatomic, strong) NSDate *deadline;
@property(nonatomic, strong) NSDate *identityRecoveryDeadline;
@property(nonatomic, strong) NSDate *resumeFenceDeadline;
@property(nonatomic, assign) NSTimeInterval started;
@property(nonatomic, assign) BOOL scriptStarted;
@property(nonatomic, assign) BOOL identityPrinted;
@property(nonatomic, copy) NSString *prompt;
@property(nonatomic, copy) NSString *profile;
@property(nonatomic, copy) NSString *conversationId;
@property(nonatomic, copy) NSString *parentMessageId;
@property(nonatomic, copy) NSString *modelSlug;
@property(nonatomic, copy) NSString *thinkingEffort;
@property(nonatomic, copy) NSString *attachmentsBase64;
@property(nonatomic, copy) NSString *handoffAttemptId;
@property(nonatomic, assign) NSInteger attachmentCount;
@property(nonatomic, assign) BOOL temporary;
@property(nonatomic, assign) BOOL proxyProtectedWrite;
@property(nonatomic, assign) BOOL proxyFetchEnded;
@property(nonatomic, assign) NSInteger proxyFetchStatus;
@property(nonatomic, copy) NSString *proxyFetchError;
@property(nonatomic, assign) BOOL realPage;
@property(nonatomic, assign) BOOL realPageFilled;
@property(nonatomic, assign) BOOL realPageSent;
@property(nonatomic, assign) BOOL domFallbackInstalled;
@property(nonatomic, assign) NSInteger realPageFillAttempts;
@property(nonatomic, strong) NSDate *nextRealPageFillAttemptAt;
@property(nonatomic, assign) NSInteger composerReadyStablePolls;
@property(nonatomic, strong) NSDate *nextComposerReadyPollAt;
@property(nonatomic, assign) NSInteger baselineAssistantCount;
@property(nonatomic, copy) NSString *baselineAssistantMessageId;
@property(nonatomic, copy) NSString *baselineAssistantText;
@end
@implementation WKTurnBrokerEntry
@end

@interface WKTurnBrokerPage : NSObject
@property(nonatomic, strong) WKWebView *webView;
@property(nonatomic, strong) NSWindow *window;
@property(nonatomic, strong) WKAuthorityDelegate *rootDelegate;
@property(nonatomic, copy) NSString *lifecycleId;
@property(nonatomic, assign) BOOL realPage;
@end
@implementation WKTurnBrokerPage
@end

static WKWebViewConfiguration *TurnBrokerConfiguration(
    WKTurnBrokerRouter *router,
    NSString *shellSource,
    BOOL realPage
) {
    WKWebViewConfiguration *configuration = [WKWebViewConfiguration new];
    configuration.websiteDataStore = [WKWebsiteDataStore defaultDataStore];
    [configuration.userContentController addScriptMessageHandler:router name:@"cwaCanonical"];
    [configuration.userContentController addScriptMessageHandler:router name:@"cwaSubmit"];
    [configuration.userContentController addScriptMessageHandler:router name:@"cwaStream"];
    [configuration.userContentController addScriptMessageHandler:router name:@"cwaProxyFetch"];
    [configuration.userContentController addUserScript:[[WKUserScript alloc]
        initWithSource:@"window.__CWA_BROKER_MANAGED__=true;"
        injectionTime:WKUserScriptInjectionTimeAtDocumentStart
        forMainFrameOnly:NO]];
    if (realPage) {
        [configuration.userContentController addUserScript:[[WKUserScript alloc]
            initWithSource:SubmitObservationScript()
            injectionTime:WKUserScriptInjectionTimeAtDocumentStart
            forMainFrameOnly:NO]];
        [configuration.userContentController addUserScript:[[WKUserScript alloc]
            initWithSource:PassiveStreamObservationScript()
            injectionTime:WKUserScriptInjectionTimeAtDocumentStart
            forMainFrameOnly:NO]];
    } else {
        [configuration.userContentController addUserScript:[[WKUserScript alloc]
            initWithSource:@"window.__CWA_DEFER_SUBMIT_OBSERVER__=true;"
            injectionTime:WKUserScriptInjectionTimeAtDocumentStart
            forMainFrameOnly:NO]];
        [configuration.userContentController addUserScript:[[WKUserScript alloc]
            initWithSource:SubmitObservationScript()
            injectionTime:WKUserScriptInjectionTimeAtDocumentStart
            forMainFrameOnly:NO]];
        [configuration.userContentController addUserScript:[[WKUserScript alloc]
            initWithSource:shellSource
            injectionTime:WKUserScriptInjectionTimeAtDocumentStart
            forMainFrameOnly:NO]];
    }
    return configuration;
}

static WKTurnBrokerPage *CreateTurnBrokerPage(
    WKTurnBrokerRouter *router,
    NSString *shellSource,
    NSString *urlString,
    NSString *lifecycleId,
    BOOL waitForReady,
    BOOL realPage
) {
    WKWebViewConfiguration *configuration = TurnBrokerConfiguration(
        router,
        shellSource,
        realPage
    );
    WKAuthorityDelegate *rootDelegate = [WKAuthorityDelegate new];
    WKWebView *webView = [[WKWebView alloc]
        initWithFrame:NSMakeRect(0, 0, 1000, 700)
        configuration:configuration];
    NSWindow *window = [[NSWindow alloc]
        initWithContentRect:NSMakeRect(-20000, -20000, 1000, 700)
        styleMask:NSWindowStyleMaskBorderless
        backing:NSBackingStoreBuffered
        defer:NO];
    window.contentView = webView;
    [window orderFront:nil];
    rootDelegate.webView = webView;
    rootDelegate.window = window;
    rootDelegate.attachmentPaths = @[];
    webView.navigationDelegate = rootDelegate;
    webView.UIDelegate = rootDelegate;

    NSURL *url = [NSURL URLWithString:urlString ?: @"https://chatgpt.com/"];
    if (url == nil) {
        [window orderOut:nil];
        return nil;
    }
    NSURLRequest *initialRequest = [NSURLRequest requestWithURL:url];
    if (realPage) {
        [webView loadRequest:initialRequest];
    } else {
        [webView loadSimulatedRequest:initialRequest responseHTMLString:MinimalSecurityShellHTML()];
    }
    if (waitForReady) {
        NSDate *readyDeadline = [NSDate dateWithTimeIntervalSinceNow:15.0];
        while (!rootDelegate.navigationFinished && [readyDeadline timeIntervalSinceNow] > 0) {
            RunLoopFor(0.02);
        }
        if (!rootDelegate.navigationFinished) {
            [window orderOut:nil];
            return nil;
        }
    }

    WKTurnBrokerPage *page = [WKTurnBrokerPage new];
    page.webView = webView;
    page.window = window;
    page.rootDelegate = rootDelegate;
    page.lifecycleId = lifecycleId ?: @"";
    page.realPage = realPage;
    return page;
}

static void CloseTurnBrokerPage(WKTurnBrokerPage *page) {
    if (page == nil) return;
    WKWebView *webView = page.webView;
    NSWindow *window = page.window;
    WKAuthorityDelegate *delegate = page.rootDelegate;
    if (webView != nil) {
        [webView stopLoading];
        webView.navigationDelegate = nil;
        webView.UIDelegate = nil;
    }
    if (window != nil) {
        if (window.firstResponder == webView) {
            [window makeFirstResponder:nil];
        }
        window.contentView = nil;
        [window orderOut:nil];
    }
    [webView removeFromSuperview];
    delegate.webView = nil;
    delegate.window = nil;
    delegate.attachmentPaths = @[];
    page.webView = nil;
    page.window = nil;
    page.rootDelegate = nil;
}

static void TurnBrokerEmitResult(NSString *requestId, NSDictionary *result) {
    PrintEvent(@{
        @"type": @"broker_result",
        @"request_id": requestId ?: @"",
        @"result": result ?: @{}
    });
}

static NSDictionary *TurnBrokerFailure(NSString *error, NSString *detail, NSString *stage, NSInteger status) {
    return @{
        @"ok": @NO,
        @"error": error ?: @"WKWEBVIEW_TURN_BROKER_FAILED",
        @"detail": detail ?: @"",
        @"stage": stage ?: @"",
        @"status": @(status)
    };
}

static NSDictionary *TurnBrokerSuccessPayload(WKTurnBrokerEntry *entry, BOOL identityRecoveryRequired) {
    WKAuthorityDelegate *delegate = entry.delegate;
    NSDictionary *successTransportSnapshot = entry.webView != nil
        ? ParseJSONResult(EvaluateSync(entry.webView, TransportDiagnosticsScript(), 1.0, nil))
        : @{};
    NSDictionary *successDOMSnapshot = entry.webView != nil
        ? ParseJSONResult(EvaluateSync(entry.webView, DOMStreamSnapshotScript(), 1.0, nil))
        : @{};
    BOOL responseOK = (delegate.streamResponseObserved && delegate.streamStatus >= 200 && delegate.streamStatus < 300)
        || (delegate.submitResponseObserved && delegate.submitStatus >= 200 && delegate.submitStatus < 300);
    BOOL terminalCompletionFence = responseOK
        && (entry.conversationId.length > 0 || entry.temporary)
        && delegate.streamTerminalObserved
        && delegate.streamConversationId.length > 0;
    BOOL topicHandoffFence = responseOK
        && delegate.streamTopicId.length > 0
        && delegate.streamConversationId.length > 0
        && (entry.conversationId.length == 0 || [delegate.streamConversationId isEqualToString:entry.conversationId]);
    BOOL resumeFenceObserved = responseOK
        && delegate.streamResumeToken.length > 0
        && delegate.streamConversationId.length > 0;
    NSString *resultConversationId = delegate.streamConversationId.length > 0
        ? delegate.streamConversationId
        : (entry.conversationId ?: @"");
    NSInteger responseStatus = delegate.streamResponseObserved ? delegate.streamStatus : delegate.submitStatus;
    NSMutableDictionary *result = [@{
        @"ok": @YES,
        @"identity_recovery_required": @(identityRecoveryRequired),
        @"client_message_id": delegate.streamClientMessageId ?: @"",
        @"assistant_message_id": delegate.streamAssistantMessageId ?: @"",
        @"conversation_id": resultConversationId,
        @"response_status": @(responseStatus),
        @"submit_request_observed": @(delegate.submitRequestObserved),
        @"submit_temporary_mode_observed": @(delegate.submitTemporaryModeObserved),
        @"submit_response_observed": @(delegate.submitResponseObserved),
        @"submit_response_status": @(delegate.submitStatus),
        @"submit_proxy_dispatch": @(delegate.submitProxyDispatch),
        @"submit_parent_message_id": delegate.submitParentMessageId ?: @"",
        @"submit_parent_match": @(delegate.submitParentMatch),
        @"submit_model": delegate.submitModel ?: @"",
        @"submit_thinking_effort": delegate.submitThinkingEffort ?: @"",
        @"stream_response_observed": @(delegate.streamResponseObserved),
        @"stream_response_status": @(delegate.streamStatus),
        @"attachment_count": @(MAX(0, entry.attachmentCount)),
        @"write_commit_proven": identityRecoveryRequired ? @NO : @YES,
        @"canonical_committed": @NO,
        @"canonical_final_completed": @NO,
        @"canonical_body_base64": @"",
        @"committed_current_node": @"",
        @"stream_started": @(delegate.streamStarted),
        @"stream_ended": @(delegate.streamEnded),
        @"stream_terminal_observed": @(delegate.streamTerminalObserved),
        @"stream_global_completion_observed": @(delegate.streamGlobalCompletionObserved),
        @"stream_resume_present": @(resumeFenceObserved),
        @"stream_resume_handoff_written": @(resumeFenceObserved),
        @"stream_handoff_observed": @(delegate.streamHandoffObserved),
        @"stream_handoff_released": @(delegate.streamHandoffReleased),
        @"stream_topic_id": delegate.streamTopicId ?: @"",
        @"turn_exchange_id": delegate.streamTurnExchangeId ?: @"",
        @"stream_conversation_id": resultConversationId,
        @"success_resource_summary": [successTransportSnapshot[@"resource_summary"] description] ?: @"",
        @"success_passive_summary": [successTransportSnapshot[@"passive_summary"] description] ?: @"",
        @"success_dom_stop": @([successDOMSnapshot[@"stop"] boolValue]),
        @"success_dom_assistant_count": @([successDOMSnapshot[@"assistantCount"] integerValue]),
        @"success_dom_message_id": [successDOMSnapshot[@"messageId"] description] ?: @"",
        @"success_dom_text_len": @([[successDOMSnapshot[@"text"] description] length]),
        @"baseline_assistant_count": @(entry.baselineAssistantCount),
        @"baseline_assistant_message_id": entry.baselineAssistantMessageId ?: @"",
        @"baseline_assistant_text_len": @(entry.baselineAssistantText.length),
        @"minimal_security_shell": @(!entry.realPage),
        @"bundle_id": @"local.gptty.webkit-authority"
    } mutableCopy];
    if (!identityRecoveryRequired) {
        NSString *proof = resumeFenceObserved ? @"RESUME_FENCE" : (topicHandoffFence ? @"TOPIC_HANDOFF_FENCE" : @"PHASE_A_TERMINAL");
        result[@"write_commit_proof"] = proof;
    }
    if (resumeFenceObserved) result[@"stream_resume_value"] = delegate.streamResumeToken;
    if (delegate.streamStopConduitToken.length > 0) result[@"_cwa_stop_conduit_token"] = delegate.streamStopConduitToken;
    if (delegate.streamTurnTraceId.length > 0) result[@"_cwa_stop_turn_trace_id"] = delegate.streamTurnTraceId;
    (void)terminalCompletionFence;
    return result;
}

static void LaunchTurnBrokerEntry(WKTurnBrokerEntry *entry) {
    if (entry == nil || entry.scriptStarted) return;
    entry.scriptStarted = YES;
    WKAuthorityDelegate *delegate = entry.delegate;
    WKWebView *webView = entry.webView;

    NSError *probeError = nil;
    id probeValue = EvaluateSync(
        webView,
        @"JSON.stringify({origin:location.origin,handler:!!(window.webkit&&window.webkit.messageHandlers&&window.webkit.messageHandlers.cwaCanonical),shell:typeof window.__cwaRunMinimalSecurityShell==='function',managed:window.__CWA_BROKER_MANAGED__===true})",
        2.0,
        &probeError
    );
    NSDictionary *probe = ParseJSONResult(probeValue);
    if (
        probeError != nil
        || ![probe isKindOfClass:[NSDictionary class]]
        || ![probe[@"handler"] boolValue]
        || ![probe[@"shell"] boolValue]
        || ![[probe[@"origin"] isKindOfClass:[NSString class]] ? probe[@"origin"] : @"" isEqualToString:@"https://chatgpt.com"]
    ) {
        NSString *origin = [probe[@"origin"] isKindOfClass:[NSString class]] ? probe[@"origin"] : @"";
        NSString *detail = [NSString stringWithFormat:@"origin=%@ handler=%@ shell=%@ error=%@",
            origin,
            [probe[@"handler"] boolValue] ? @"yes" : @"no",
            [probe[@"shell"] boolValue] ? @"yes" : @"no",
            probeError.localizedDescription ?: @"none"
        ];
        delegate.canonicalResult = @{
            @"ok":@NO,
            @"stage":@"broker_page_probe",
            @"error":[@"BROKER_PAGE_PROBE_FAILED:" stringByAppendingString:detail]
        };
        delegate.canonicalDone = YES;
        return;
    }

    NSDictionary *config = @{
        @"request_id": entry.requestId ?: @"",
        @"prompt": entry.prompt ?: @"",
        @"profile": entry.profile ?: @"",
        @"temporary": @(entry.temporary),
        @"proxy_protected_write": @(entry.proxyProtectedWrite),
        @"conversation_id": entry.conversationId ?: @"",
        @"parent_message_id": entry.parentMessageId ?: @"",
        @"selected_model_slug": entry.modelSlug ?: @"",
        @"selected_thinking_effort": entry.thinkingEffort ?: @"",
        @"attachments_base64": entry.attachmentsBase64 ?: @"",
        @"handoff_attempt_id": entry.handoffAttemptId ?: @""
    };
    NSData *configData = [NSJSONSerialization dataWithJSONObject:config options:0 error:nil];
    NSString *configJSON = [[NSString alloc] initWithData:configData encoding:NSUTF8StringEncoding];
    if (configJSON.length == 0) {
        delegate.canonicalResult = @{@"ok":@NO,@"stage":@"broker_page",@"error":@"BROKER_PAGE_CONFIG_INVALID"};
        delegate.canonicalDone = YES;
        return;
    }
    NSString *runScript = [NSString stringWithFormat:
        @"(()=>{if(typeof window.__cwaRunMinimalSecurityShell!=='function')throw new Error('BROKER_PAGE_SHELL_MISSING');window.__cwaRunMinimalSecurityShell(%@);return true;})()",
        configJSON
    ];
    NSError *runError = nil;
    id launched = EvaluateSync(webView, runScript, 2.0, &runError);
    if (runError != nil || ![launched respondsToSelector:@selector(boolValue)] || ![launched boolValue]) {
        NSString *detail = runError.localizedDescription ?: @"minimal shell launch returned false";
        delegate.canonicalResult = @{
            @"ok":@NO,
            @"stage":@"broker_page",
            @"error":[@"BROKER_PAGE_LAUNCH_FAILED:" stringByAppendingString:detail]
        };
        delegate.canonicalDone = YES;
    }
}

static WKTurnBrokerEntry *StartTurnBrokerEntry(
    NSDictionary *command,
    WKWebView *webView,
    WKAuthorityDelegate *pageNavigationDelegate,
    WKTurnBrokerRouter *router,
    BOOL realPage
) {
    NSString *requestId = [command[@"request_id"] isKindOfClass:[NSString class]] ? command[@"request_id"] : @"";
    NSDictionary *request = [command[@"request"] isKindOfClass:[NSDictionary class]] ? command[@"request"] : nil;
    if (requestId.length == 0 || request == nil || webView == nil) return nil;

    NSString *prompt = RequestString(request, @"prompt", @"");
    NSString *profile = [RequestString(request, @"profile", @"") uppercaseString];
    NSString *conversationId = RequestString(request, @"minimal_conversation_id", @"");
    NSString *parentMessageId = RequestString(request, @"minimal_parent_message_id", @"");
    NSString *modelSlug = RequestString(request, @"minimal_model_slug", @"");
    NSString *thinkingEffort = RequestString(request, @"minimal_thinking_effort", @"");
    NSString *handoffAttemptId = RequestString(request, @"minimal_handoff_attempt_id", @"");
    BOOL temporary = RequestBool(request, @"minimal_temporary", NO);
    BOOL proxyProtectedWrite = RequestBool(request, @"proxy_protected_write", NO);
    id rawAttachments = request[@"minimal_attachments"];
    NSString *attachmentsBase64 = [rawAttachments isKindOfClass:[NSArray class]] ? Base64JSONValue(rawAttachments) : @"";
    NSInteger attachmentCount = [rawAttachments isKindOfClass:[NSArray class]] ? (NSInteger)[(NSArray *)rawAttachments count] : 0;
    NSTimeInterval timeout = RequestDouble(request, @"timeout", 150.0);
    if (timeout <= 0) timeout = 150.0;
    if (prompt.length == 0 || ((conversationId.length > 0) != (parentMessageId.length > 0))) return nil;

    WKAuthorityDelegate *delegate = [WKAuthorityDelegate new];
    delegate.brokerRequestId = requestId;
    if (conversationId.length > 0) delegate.streamConversationId = conversationId;
    [router registerDelegate:delegate requestId:requestId];

    WKTurnBrokerEntry *entry = [WKTurnBrokerEntry new];
    entry.requestId = requestId;
    entry.request = request;
    entry.delegate = delegate;
    entry.webView = webView;
    entry.pageNavigationDelegate = pageNavigationDelegate;
    entry.started = [NSDate timeIntervalSinceReferenceDate];
    entry.deadline = [NSDate dateWithTimeIntervalSinceNow:timeout];
    entry.prompt = prompt;
    entry.profile = profile;
    entry.conversationId = conversationId;
    entry.parentMessageId = parentMessageId;
    entry.modelSlug = modelSlug;
    entry.thinkingEffort = thinkingEffort;
    entry.attachmentsBase64 = attachmentsBase64 ?: @"";
    entry.handoffAttemptId = handoffAttemptId;
    entry.attachmentCount = attachmentCount;
    entry.temporary = temporary;
    entry.proxyProtectedWrite = proxyProtectedWrite;
    entry.proxyFetchEnded = NO;
    entry.proxyFetchStatus = 0;
    entry.proxyFetchError = @"";
    entry.realPage = realPage;
    entry.realPageFilled = NO;
    entry.realPageSent = NO;
    entry.domFallbackInstalled = NO;
    entry.realPageFillAttempts = 0;
    entry.nextRealPageFillAttemptAt = nil;
    entry.composerReadyStablePolls = 0;
    entry.nextComposerReadyPollAt = nil;
    entry.baselineAssistantCount = 0;
    entry.baselineAssistantMessageId = @"";
    entry.baselineAssistantText = @"";
    entry.scriptStarted = NO;
    if (!realPage && pageNavigationDelegate.navigationFinished) LaunchTurnBrokerEntry(entry);
    return entry;

}

static BOOL PumpRealPageTurnBrokerEntry(
    WKTurnBrokerEntry *entry,
    NSDictionary **outResult
) {
    WKAuthorityDelegate *delegate = entry.delegate;
    WKWebView *webView = entry.webView;
    if (!entry.pageNavigationDelegate.navigationFinished) {
        if ([entry.deadline timeIntervalSinceNow] <= 0) {
            *outResult = TurnBrokerFailure(
                @"WKWEBVIEW_TEMPORARY_PAGE_LOAD_TIMEOUT",
                @"",
                @"real_page",
                0
            );
            return YES;
        }
        return NO;
    }
    if (entry.attachmentCount > 0) {
        *outResult = TurnBrokerFailure(
            @"WKWEBVIEW_TEMPORARY_ATTACHMENTS_NOT_SUPPORTED",
            @"",
            @"real_page",
            0
        );
        return YES;
    }

    if (!entry.scriptStarted) {
        NSString *requestLiteral = JSONStringLiteral(entry.requestId ?: @"");
        NSString *profileLiteral = JSONStringLiteral(entry.profile ?: @"");
        NSString *parentLiteral = JSONStringLiteral(entry.parentMessageId ?: @"");
        NSString *routeScript = [NSString stringWithFormat:
            @"window.__CWA_BROKER_REQUEST_ID__=%@;window.__CWA_EXPECTED_PROFILE__=%@;window.__CWA_EXPECTED_PARENT_MESSAGE_ID__=%@;if(typeof window.__cwaWKBeginTurnObserver==='function'){window.__cwaWKBeginTurnObserver(window.__CWA_BROKER_REQUEST_ID__);}true;",
            requestLiteral,
            profileLiteral,
            parentLiteral
        ];
        NSError *routeError = nil;
        id routed = EvaluateSync(webView, routeScript, 1.0, &routeError);
        if (
            routeError != nil
            || ![routed respondsToSelector:@selector(boolValue)]
            || ![routed boolValue]
        ) {
            *outResult = TurnBrokerFailure(
                @"WKWEBVIEW_TEMPORARY_REQUEST_ROUTING_FAILED",
                routeError.localizedDescription ?: @"",
                @"real_page",
                0
            );
            return YES;
        }
        entry.scriptStarted = YES;
    }

    if (!entry.realPageSent) {
        NSError *readinessError = nil;
        NSDictionary *snapshot = ParseJSONResult(
            EvaluateSync(webView, ReadinessScript(), 1.0, &readinessError)
        );
        if (readinessError != nil || snapshot == nil) {
            if ([entry.deadline timeIntervalSinceNow] <= 0) {
                *outResult = TurnBrokerFailure(
                    @"WKWEBVIEW_TEMPORARY_COMPOSER_NOT_READY",
                    readinessError.localizedDescription ?: @"",
                    @"real_page",
                    0
                );
                return YES;
            }
            return NO;
        }
        if ([snapshot[@"login"] boolValue]) {
            *outResult = TurnBrokerFailure(
                @"WKWEBVIEW_AUTHORITY_LOGIN_REQUIRED",
                @"",
                @"real_page",
                0
            );
            return YES;
        }
        if (![snapshot[@"composerReady"] boolValue]) {
            entry.composerReadyStablePolls = 0;
            entry.nextComposerReadyPollAt = nil;
            if ([entry.deadline timeIntervalSinceNow] <= 0) {
                *outResult = TurnBrokerFailure(
                    @"WKWEBVIEW_TEMPORARY_COMPOSER_NOT_READY",
                    @"",
                    @"real_page",
                    0
                );
                return YES;
            }
            EvaluateSync(webView, ScrollBottomScript(), 0.5, nil);
            return NO;
        }

        if (
            !entry.realPageFilled
            && entry.realPageFillAttempts == 0
            && entry.composerReadyStablePolls < 2
        ) {
            if (
                entry.nextComposerReadyPollAt != nil
                && [entry.nextComposerReadyPollAt timeIntervalSinceNow] > 0
            ) {
                return NO;
            }
            entry.composerReadyStablePolls += 1;
            if (entry.composerReadyStablePolls < 2) {
                entry.nextComposerReadyPollAt = [
                    NSDate dateWithTimeIntervalSinceNow:0.25
                ];
                return NO;
            }
            entry.nextComposerReadyPollAt = nil;
        }

        if (!entry.realPageFilled) {
            if (
                entry.nextRealPageFillAttemptAt != nil
                && [entry.nextRealPageFillAttemptAt timeIntervalSinceNow] > 0
            ) {
                return NO;
            }
            if (entry.realPageFillAttempts == 0) {
                NSDictionary *baseline = ParseJSONResult(
                    EvaluateSync(webView, DOMStreamSnapshotScript(), 1.0, nil)
                );
                entry.baselineAssistantCount = [baseline[@"assistantCount"] integerValue];
                entry.baselineAssistantMessageId =
                    [baseline[@"messageId"] isKindOfClass:[NSString class]]
                        ? baseline[@"messageId"]
                        : @"";
                entry.baselineAssistantText =
                    [baseline[@"text"] isKindOfClass:[NSString class]]
                        ? baseline[@"text"]
                        : @"";
            }
            entry.realPageFillAttempts += 1;
            NSDictionary *filled = NativeFillComposer(webView, entry.prompt);
            if (filled == nil) {
                filled = ParseJSONResult(
                    EvaluateSync(webView, FillScript(entry.prompt), 2.0, nil)
                );
            }
            NSString *filledText = [filled[@"text"] isKindOfClass:[NSString class]]
                ? filled[@"text"]
                : @"";
            if (![filled[@"ok"] boolValue] || filledText.length == 0) {
                if (entry.realPageFillAttempts < 6) {
                    entry.realPageFilled = NO;
                    entry.nextRealPageFillAttemptAt = [
                        NSDate dateWithTimeIntervalSinceNow:MIN(1.0, 0.2 * (double)entry.realPageFillAttempts)
                    ];
                    return NO;
                }
                NSDictionary *composerDiag = ParseJSONResult(
                    EvaluateSync(webView, ComposerDiagnosticsScript(), 1.0, nil)
                );
                NSDictionary *selected = [composerDiag[@"selected"] isKindOfClass:[NSDictionary class]]
                    ? composerDiag[@"selected"]
                    : @{};
                NSArray *candidateRows = [composerDiag[@"candidates"] isKindOfClass:[NSArray class]]
                    ? composerDiag[@"candidates"]
                    : @[];
                NSInteger visibleCandidates = 0;
                for (id row in candidateRows) {
                    if ([row isKindOfClass:[NSDictionary class]] && [row[@"visible"] boolValue]) {
                        visibleCandidates += 1;
                    }
                }
                NSString *detail = [NSString stringWithFormat:
                    @"composer did not retain text attempts=%ld selected_tag=%@ id=%@ test=%@ lexical=%@ role=%@ ce=%@ active=%d connected=%d w=%ld h=%ld text_len=%ld form=%d scope=%@ candidates=%ld visible=%ld",
                    (long)entry.realPageFillAttempts,
                    [selected[@"tag"] description] ?: @"",
                    [selected[@"id"] description] ?: @"",
                    [selected[@"test"] description] ?: @"",
                    [selected[@"lexical"] description] ?: @"",
                    [selected[@"role"] description] ?: @"",
                    [selected[@"ce"] description] ?: @"",
                    [selected[@"active"] boolValue],
                    [selected[@"connected"] boolValue],
                    (long)[selected[@"w"] integerValue],
                    (long)[selected[@"h"] integerValue],
                    (long)[selected[@"text_len"] integerValue],
                    [selected[@"form"] boolValue],
                    [selected[@"scope_test"] description] ?: @"",
                    (long)candidateRows.count,
                    (long)visibleCandidates
                ];
                *outResult = TurnBrokerFailure(
                    @"WKWEBVIEW_TEMPORARY_FILL_FAILED",
                    detail,
                    @"real_page",
                    0
                );
                return YES;
            }
            entry.realPageFilled = YES;
            entry.nextRealPageFillAttemptAt = nil;
            return NO;
        }

        NSDictionary *sendReady = ParseJSONResult(
            EvaluateSync(webView, ReadinessScript(), 1.0, nil)
        );
        if (![sendReady[@"send"] boolValue]) {
            if (
                !delegate.submitRequestObserved
                && [sendReady[@"composerTextLength"] integerValue] == 0
                && entry.realPageFillAttempts < 6
            ) {
                entry.realPageFilled = NO;
                entry.nextRealPageFillAttemptAt = [
                    NSDate dateWithTimeIntervalSinceNow:MIN(1.0, 0.2 * (double)entry.realPageFillAttempts)
                ];
                return NO;
            }
            if ([entry.deadline timeIntervalSinceNow] <= 0) {
                NSString *detail = [NSString stringWithFormat:
                    @"composer_ready=%d send=%d stop=%d composer_text_len=%ld latest_parent_match=%d",
                    [sendReady[@"composerReady"] boolValue],
                    [sendReady[@"send"] boolValue],
                    [sendReady[@"stop"] boolValue],
                    (long)[sendReady[@"composerTextLength"] integerValue],
                    entry.parentMessageId.length == 0
                        || [[sendReady[@"latestMessageId"] description]
                            isEqualToString:entry.parentMessageId]
                ];
                *outResult = TurnBrokerFailure(
                    @"WKWEBVIEW_TEMPORARY_SEND_NOT_READY",
                    detail,
                    @"real_page",
                    0
                );
                return YES;
            }
            return NO;
        }
        if (!entry.domFallbackInstalled) {
            NSError *domObserverError = nil;
            id domObserverInstalled = EvaluateSync(
                webView,
                InstallDOMFallbackScript(
                    entry.requestId,
                    entry.baselineAssistantCount,
                    entry.baselineAssistantMessageId,
                    entry.baselineAssistantText
                ),
                2.0,
                &domObserverError
            );
            if (
                domObserverError != nil
                || ![domObserverInstalled respondsToSelector:@selector(boolValue)]
                || ![domObserverInstalled boolValue]
            ) {
                *outResult = TurnBrokerFailure(
                    @"WKWEBVIEW_TEMPORARY_DOM_OBSERVER_INSTALL_FAILED",
                    domObserverError.localizedDescription ?: @"",
                    @"real_page",
                    0
                );
                return YES;
            }
            entry.domFallbackInstalled = YES;
        }
        NSString *proxyModeScript = [NSString stringWithFormat:
            @"window.__CWA_PROXY_PROTECTED_WRITE__=%@;true;",
            entry.proxyProtectedWrite ? @"true" : @"false"
        ];
        EvaluateSync(webView, proxyModeScript, 0.5, nil);
        NSError *rearmError = nil;
        NSDictionary *rearmed = ParseJSONResult(
            EvaluateSync(webView, RearmFetchObserversScript(), 1.0, &rearmError)
        );
        if (rearmError != nil || ![rearmed[@"ok"] boolValue]) {
            NSString *detail = [NSString stringWithFormat:
                @"submit=%d stream=%d submit_attached=%d stream_attached=%d error=%@",
                [rearmed[@"submit"] boolValue],
                [rearmed[@"stream"] boolValue],
                [rearmed[@"submitAttached"] boolValue],
                [rearmed[@"streamAttached"] boolValue],
                rearmError.localizedDescription ?: @""
            ];
            *outResult = TurnBrokerFailure(
                @"WKWEBVIEW_TEMPORARY_OBSERVER_REARM_FAILED",
                detail,
                @"real_page",
                0
            );
            return YES;
        }
        NSDictionary *sent = NativeClickSendButton(webView);
        if (![sent[@"ok"] boolValue]) {
            *outResult = TurnBrokerFailure(
                @"WKWEBVIEW_TEMPORARY_SEND_FAILED",
                [sent[@"reason"] description] ?: @"",
                @"real_page",
                0
            );
            return YES;
        }
        entry.realPageSent = YES;
        return NO;
    }

    if (delegate.submitRequestObserved && !delegate.submitTemporaryModeObserved) {
        *outResult = TurnBrokerFailure(
            @"WKWEBVIEW_TEMPORARY_MODE_NOT_OBSERVED",
            @"",
            @"real_page",
            delegate.submitStatus
        );
        return YES;
    }
    if (
        delegate.submitRequestObserved
        && entry.profile.length > 0
        && !delegate.submitProfileMatch
    ) {
        NSString *detail = [NSString stringWithFormat:
            @"requested=%@ model=%@ thinking_effort=%@",
            entry.profile,
            delegate.submitModel ?: @"",
            delegate.submitThinkingEffort ?: @""
        ];
        *outResult = TurnBrokerFailure(
            @"WKWEBVIEW_PROFILE_NOT_PROVEN",
            detail,
            @"real_page",
            delegate.submitStatus
        );
        return YES;
    }
    if (
        delegate.submitRequestObserved
        && entry.parentMessageId.length > 0
        && !delegate.submitParentMatch
    ) {
        NSString *detail = [NSString stringWithFormat:
            @"expected=%@ observed=%@",
            entry.parentMessageId,
            delegate.submitParentMessageId ?: @""
        ];
        *outResult = TurnBrokerFailure(
            @"WKWEBVIEW_PARENT_NOT_PROVEN",
            detail,
            @"real_page",
            delegate.submitStatus
        );
        return YES;
    }

    if (entry.proxyProtectedWrite && entry.proxyFetchError.length > 0) {
        *outResult = TurnBrokerFailure(
            @"WKWEBVIEW_TEMPORARY_PROXY_FETCH_FAILED",
            entry.proxyFetchError,
            @"real_page",
            entry.proxyFetchStatus
        );
        return YES;
    }

    BOOL proxyProductSettled = !entry.proxyProtectedWrite;
    if (entry.proxyProtectedWrite && entry.proxyFetchEnded) {
        NSDictionary *settledSnapshot = ParseJSONResult(
            EvaluateSync(webView, ReadinessScript(), 1.0, nil)
        );
        proxyProductSettled = [settledSnapshot[@"composerReady"] boolValue]
            && ![settledSnapshot[@"stop"] boolValue];
    }

    if (
        delegate.submitRequestObserved
        && delegate.submitTemporaryModeObserved
        && (entry.profile.length == 0 || delegate.submitProfileMatch)
        && (entry.parentMessageId.length == 0 || delegate.submitParentMatch)
        && proxyProductSettled
        && delegate.streamTerminalObserved
        && delegate.streamConversationId.length > 0
        && delegate.streamAssistantMessageId.length > 0
    ) {
        if (
            entry.conversationId.length > 0
            && ![delegate.streamConversationId isEqualToString:entry.conversationId]
        ) {
            *outResult = TurnBrokerFailure(
                @"WKWEBVIEW_TEMPORARY_CONVERSATION_MISMATCH",
                @"",
                @"real_page",
                delegate.streamStatus
            );
            return YES;
        }
        *outResult = TurnBrokerSuccessPayload(entry, NO);
        return YES;
    }

    if ([entry.deadline timeIntervalSinceNow] <= 0) {
        NSDictionary *domSnapshot = ParseJSONResult(
            EvaluateSync(webView, DOMStreamSnapshotScript(), 1.0, nil)
        );
        NSDictionary *transportSnapshot = ParseJSONResult(
            EvaluateSync(webView, TransportDiagnosticsScript(), 1.0, nil)
        );
        NSDictionary *domFallbackSnapshot = ParseJSONResult(
            EvaluateSync(webView, DOMFallbackDiagnosticsScript(entry.requestId), 1.0, nil)
        );
        if (
            delegate.submitRequestObserved
            && delegate.submitTemporaryModeObserved
            && (entry.profile.length == 0 || delegate.submitProfileMatch)
            && (entry.parentMessageId.length == 0 || delegate.submitParentMatch)
            && proxyProductSettled
            && delegate.streamTerminalObserved
            && delegate.streamConversationId.length > 0
            && delegate.streamAssistantMessageId.length > 0
        ) {
            if (
                entry.conversationId.length > 0
                && ![delegate.streamConversationId isEqualToString:entry.conversationId]
            ) {
                *outResult = TurnBrokerFailure(
                    @"WKWEBVIEW_TEMPORARY_CONVERSATION_MISMATCH",
                    @"",
                    @"real_page",
                    delegate.streamStatus
                );
                return YES;
            }
            *outResult = TurnBrokerSuccessPayload(entry, NO);
            return YES;
        }
        NSString *domText = [domSnapshot[@"text"] isKindOfClass:[NSString class]]
            ? domSnapshot[@"text"]
            : @"";
        NSString *domTextPreview = domText.length > 180
            ? [[domText substringToIndex:180] stringByAppendingString:@"…"]
            : domText;
        NSString *detail = [NSString stringWithFormat:
            @"path=%@ dom_stop=%d dom_count=%ld dom_message=%@ dom_text_len=%ld dom_text=%@ baseline_count=%ld baseline_message=%@ baseline_text_len=%ld fallback=%@ resources=%@ passive=%@ hooks=%d/%d fetch=%d/%d submit=%d endpoint=%@ signal=%d aborted=%d keepalive=%d mode=%@ temporary=%d profile=%d parent_match=%d parent=%@ submit_response=%d submit_status=%ld proxy=%d submit_error=%@ stream_started=%d stream_response=%d stream_status=%ld stream_ended=%d terminal=%d global=%d raw=%ld text=%ld resume=%d conversation=%d assistant=%d service_worker=%@",
            [transportSnapshot[@"path"] description] ?: @"",
            [domSnapshot[@"stop"] boolValue],
            (long)[domSnapshot[@"assistantCount"] integerValue],
            [domSnapshot[@"messageId"] description] ?: @"",
            (long)domText.length,
            domTextPreview,
            (long)entry.baselineAssistantCount,
            entry.baselineAssistantMessageId ?: @"",
            (long)entry.baselineAssistantText.length,
            [domFallbackSnapshot description] ?: @"",
            [transportSnapshot[@"resource_summary"] description] ?: @"",
            [transportSnapshot[@"passive_summary"] description] ?: @"",
            [transportSnapshot[@"submit_observer_installed"] boolValue],
            [transportSnapshot[@"stream_observer_installed"] boolValue],
            [transportSnapshot[@"fetch_is_submit_wrapper"] boolValue],
            [transportSnapshot[@"fetch_is_stream_wrapper"] boolValue],
            delegate.submitRequestObserved,
            delegate.submitEndpoint ?: @"",
            delegate.submitSignalPresent,
            delegate.submitSignalAborted,
            delegate.submitKeepalive,
            delegate.submitRequestMode ?: @"",
            delegate.submitTemporaryModeObserved,
            delegate.submitProfileMatch,
            delegate.submitParentMatch,
            delegate.submitParentMessageId ?: @"",
            delegate.submitResponseObserved,
            (long)delegate.submitStatus,
            delegate.submitProxyDispatch,
            delegate.submitError ?: @"",
            delegate.streamStarted,
            delegate.streamResponseObserved,
            (long)delegate.streamStatus,
            delegate.streamEnded,
            delegate.streamTerminalObserved,
            delegate.streamGlobalCompletionObserved,
            (long)delegate.streamRawEventCount,
            (long)delegate.streamTextEventCount,
            delegate.streamResumeToken.length > 0,
            delegate.streamConversationId.length > 0,
            delegate.streamAssistantMessageId.length > 0,
            [transportSnapshot[@"service_worker"] description] ?: @""
        ];
        *outResult = TurnBrokerFailure(
            @"WKWEBVIEW_TEMPORARY_STREAM_FENCE_MISSING",
            detail,
            @"real_page",
            delegate.streamStatus ?: delegate.submitStatus
        );
        return YES;
    }
    return NO;
}

static BOOL PumpTurnBrokerEntry(WKTurnBrokerEntry *entry, NSDictionary **outResult) {
    if (entry.realPage) {
        return PumpRealPageTurnBrokerEntry(entry, outResult);
    }
    WKAuthorityDelegate *delegate = entry.delegate;
    if (!entry.scriptStarted && entry.pageNavigationDelegate.navigationFinished) {
        LaunchTurnBrokerEntry(entry);
    }

    NSDictionary *launch = delegate.canonicalResult;
    if (delegate.canonicalDone && [launch isKindOfClass:[NSDictionary class]] && ![launch[@"ok"] boolValue]) {
        *outResult = TurnBrokerFailure(
            @"WKWEBVIEW_MINIMAL_SECURITY_WRITE_FAILED",
            [launch[@"error"] isKindOfClass:[NSString class]] ? launch[@"error"] : @"",
            [launch[@"stage"] isKindOfClass:[NSString class]] ? launch[@"stage"] : @"",
            [launch[@"status"] respondsToSelector:@selector(integerValue)] ? [launch[@"status"] integerValue] : 0
        );
        return YES;
    }

    if (!entry.identityPrinted && delegate.streamConversationId.length > 0 && delegate.submitRequestObserved) {
        PrintEventForRequest(@{
            @"type": @"write_identity_resolved",
            @"conversation_id": delegate.streamConversationId,
            @"submit_response_observed": @(delegate.submitResponseObserved),
            @"submit_response_status": @(delegate.submitStatus)
        }, entry.requestId);
        entry.identityPrinted = YES;
    }

    BOOL responseOK = (delegate.streamResponseObserved && delegate.streamStatus >= 200 && delegate.streamStatus < 300)
        || (delegate.submitResponseObserved && delegate.submitStatus >= 200 && delegate.submitStatus < 300);
    if (entry.identityRecoveryDeadline == nil && responseOK && (entry.conversationId.length > 0 || delegate.streamClientMessageId.length > 0)) {
        entry.identityRecoveryDeadline = [NSDate dateWithTimeIntervalSinceNow:8.0];
    }
    BOOL terminalCompletionFence = responseOK
        && (entry.conversationId.length > 0 || entry.temporary)
        && delegate.streamTerminalObserved
        && delegate.streamConversationId.length > 0;
    BOOL topicHandoffFence = responseOK
        && delegate.streamTopicId.length > 0
        && delegate.streamConversationId.length > 0
        && (entry.conversationId.length == 0 || [delegate.streamConversationId isEqualToString:entry.conversationId]);
    BOOL resumeFenceObserved = responseOK
        && delegate.streamResumeToken.length > 0
        && delegate.streamConversationId.length > 0;
    BOOL temporaryHandoffReleased = entry.temporary
        && resumeFenceObserved
        && delegate.streamHandoffReleased;
    if (terminalCompletionFence || (!entry.temporary && topicHandoffFence) || temporaryHandoffReleased) {
        *outResult = TurnBrokerSuccessPayload(entry, NO);
        return YES;
    }
    if (!entry.temporary && resumeFenceObserved && entry.resumeFenceDeadline == nil) {
        entry.resumeFenceDeadline = [NSDate dateWithTimeIntervalSinceNow:0.5];
    }
    if (
        !entry.temporary
        && resumeFenceObserved
        && entry.resumeFenceDeadline != nil
        && [entry.resumeFenceDeadline timeIntervalSinceNow] <= 0
    ) {
        *outResult = TurnBrokerSuccessPayload(entry, NO);
        return YES;
    }
    if (!entry.temporary && entry.identityRecoveryDeadline != nil && [entry.identityRecoveryDeadline timeIntervalSinceNow] <= 0) {
        *outResult = TurnBrokerSuccessPayload(entry, YES);
        return YES;
    }
    if ([entry.deadline timeIntervalSinceNow] <= 0) {
        *outResult = TurnBrokerFailure(@"WKWEBVIEW_MINIMAL_SECURITY_STREAM_FENCE_MISSING", @"", @"write", delegate.streamStatus ?: delegate.submitStatus);
        return YES;
    }
    return NO;
}

static BOOL DeliverProxyFetchCommand(WKTurnBrokerEntry *entry, NSDictionary *command) {
    if (entry == nil || entry.webView == nil) return NO;
    NSString *type = RequestString(command, @"type", @"");
    NSString *proxyId = RequestString(command, @"proxy_id", @"");
    if (proxyId.length == 0) return NO;
    NSString *proxyLiteral = JSONStringLiteral(proxyId);
    NSString *script = nil;
    if ([type isEqualToString:@"proxy_fetch_headers"]) {
        NSInteger status = RequestInteger(command, @"status", 200);
        id rawHeaders = command[@"headers"];
        NSDictionary *headers = [rawHeaders isKindOfClass:[NSDictionary class]] ? rawHeaders : @{};
        NSData *data = [NSJSONSerialization dataWithJSONObject:headers options:0 error:nil];
        NSString *headersJSON = data != nil
            ? [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding]
            : @"{}";
        script = [NSString stringWithFormat:
            @"(()=>{const f=window.__cwaProxyFetchHeaders;return typeof f==='function'&&f(%@,%ld,%@);})()",
            proxyLiteral,
            (long)status,
            headersJSON ?: @"{}"
        ];
    } else if ([type isEqualToString:@"proxy_fetch_chunk"]) {
        NSString *chunk = RequestString(command, @"chunk_base64", @"");
        script = [NSString stringWithFormat:
            @"(()=>{const f=window.__cwaProxyFetchChunk;return typeof f==='function'&&f(%@,%@);})()",
            proxyLiteral,
            JSONStringLiteral(chunk)
        ];
    } else if ([type isEqualToString:@"proxy_fetch_end"]) {
        script = [NSString stringWithFormat:
            @"(()=>{const f=window.__cwaProxyFetchEnd;return typeof f==='function'&&f(%@);})()",
            proxyLiteral
        ];
    } else if ([type isEqualToString:@"proxy_fetch_error"]) {
        NSString *message = RequestString(command, @"message", @"WKWEBVIEW_PROXY_FETCH_FAILED");
        script = [NSString stringWithFormat:
            @"(()=>{const f=window.__cwaProxyFetchError;return typeof f==='function'&&f(%@,%@);})()",
            proxyLiteral,
            JSONStringLiteral(message)
        ];
    } else {
        return NO;
    }
    NSError *error = nil;
    id result = EvaluateSync(entry.webView, script, 2.0, &error);
    return error == nil && [result respondsToSelector:@selector(boolValue)] && [result boolValue];
}

static void FinishTurnBrokerEntry(WKTurnBrokerEntry *entry, WKTurnBrokerRouter *router) {
    if (entry == nil) return;
    if (entry.realPage && entry.webView != nil && entry.requestId.length > 0) {
        if (entry.domFallbackInstalled) {
            EvaluateSync(
                entry.webView,
                RemoveDOMFallbackScript(entry.requestId),
                0.5,
                nil
            );
        }
        NSString *requestLiteral = JSONStringLiteral(entry.requestId);
        NSString *clearScript = [NSString stringWithFormat:
            @"if(window.__CWA_BROKER_REQUEST_ID__===%@){if(typeof window.__cwaWKEndTurnObserver==='function'){window.__cwaWKEndTurnObserver(window.__CWA_BROKER_REQUEST_ID__);}window.__CWA_BROKER_REQUEST_ID__='';window.__CWA_EXPECTED_PROFILE__='';window.__CWA_EXPECTED_PARENT_MESSAGE_ID__='';}true;",
            requestLiteral
        ];
        EvaluateSync(entry.webView, clearScript, 0.5, nil);
    }
    [router removeRequestId:entry.requestId];
}
static int RunTurnBroker(void) {
    [NSApplication sharedApplication];
    NSString *shellSource = MinimalSecurityShellSource();
    if (shellSource.length == 0) {
        PrintEvent(@{@"type": @"broker_failed", @"error": @"WKWEBVIEW_MINIMAL_SECURITY_SCRIPT_MISSING"});
        return 70;
    }

    WKTurnBrokerRouter *router = [WKTurnBrokerRouter new];
    WKTurnBrokerPage *normalPage = CreateTurnBrokerPage(
        router,
        shellSource,
        @"https://chatgpt.com/",
        @"",
        YES,
        NO
    );
    if (normalPage == nil) {
        PrintEvent(@{@"type": @"broker_failed", @"error": @"WKWEBVIEW_TURN_BROKER_ROOT_TIMEOUT"});
        return 71;
    }

    __block BOOL shouldExit = NO;
    __block NSMutableDictionary<NSString *, WKTurnBrokerEntry *> *entries = [NSMutableDictionary dictionary];
    __block NSMutableDictionary<NSString *, WKTurnBrokerPage *> *temporaryPages = [NSMutableDictionary dictionary];
    NSFileHandle *input = [NSFileHandle fileHandleWithStandardInput];
    __block NSMutableData *buffer = [NSMutableData data];
    input.readabilityHandler = ^(NSFileHandle *handle) {
        NSData *chunk = [handle availableData];
        if (chunk.length == 0) {
            dispatch_async(dispatch_get_main_queue(), ^{ shouldExit = YES; });
            return;
        }
        @synchronized (buffer) {
            [buffer appendData:chunk];
            while (buffer.length > 0) {
                const uint8_t *bytes = buffer.bytes;
                NSUInteger newline = NSNotFound;
                for (NSUInteger idx = 0; idx < buffer.length; idx++) {
                    if (bytes[idx] == '\n') { newline = idx; break; }
                }
                if (newline == NSNotFound) break;
                NSData *line = [buffer subdataWithRange:NSMakeRange(0, newline)];
                [buffer replaceBytesInRange:NSMakeRange(0, newline + 1) withBytes:NULL length:0];
                if (line.length == 0) continue;
                id parsed = [NSJSONSerialization JSONObjectWithData:line options:0 error:nil];
                if (![parsed isKindOfClass:[NSDictionary class]]) continue;
                NSDictionary *command = (NSDictionary *)parsed;
                dispatch_async(dispatch_get_main_queue(), ^{
                    NSString *type = [command[@"type"] isKindOfClass:[NSString class]] ? command[@"type"] : @"";
                    NSString *requestId = [command[@"request_id"] isKindOfClass:[NSString class]] ? command[@"request_id"] : @"";
                    if ([type isEqualToString:@"shutdown"]) {
                        shouldExit = YES;
                        return;
                    }
                    if ([type isEqualToString:@"cancel"]) {
                        WKTurnBrokerEntry *entry = entries[requestId];
                        if (entry != nil) {
                            FinishTurnBrokerEntry(entry, router);
                            [entries removeObjectForKey:requestId];
                        }
                        return;
                    }
                    if ([type isEqualToString:@"end_temporary_lifecycle"]) {
                        NSString *lifecycleId = RequestString(command, @"lifecycle_id", @"");
                        if (requestId.length == 0 || lifecycleId.length == 0) {
                            TurnBrokerEmitResult(requestId, TurnBrokerFailure(@"WKWEBVIEW_TEMPORARY_LIFECYCLE_CLOSE_INVALID", @"", @"broker", 0));
                            return;
                        }
                        WKTurnBrokerPage *page = temporaryPages[lifecycleId];
                        if (page != nil) {
                            for (NSString *activeRequestId in [entries.allKeys copy]) {
                                WKTurnBrokerEntry *activeEntry = entries[activeRequestId];
                                if (activeEntry != nil && activeEntry.webView == page.webView) {
                                    FinishTurnBrokerEntry(activeEntry, router);
                                    [entries removeObjectForKey:activeRequestId];
                                }
                            }
                            CloseTurnBrokerPage(page);
                            [temporaryPages removeObjectForKey:lifecycleId];
                        }
                        TurnBrokerEmitResult(requestId, @{
                            @"ok": @YES,
                            @"temporary_lifecycle_state": @"ENDED",
                            @"lifecycle_id": lifecycleId
                        });
                        return;
                    }
                    if ([type hasPrefix:@"proxy_fetch_"]) {
                        WKTurnBrokerEntry *entry = entries[requestId];
                        if (entry != nil && entry.proxyProtectedWrite) {
                            if ([type isEqualToString:@"proxy_fetch_headers"]) {
                                entry.proxyFetchStatus = RequestInteger(command, @"status", 0);
                            } else if ([type isEqualToString:@"proxy_fetch_end"]) {
                                entry.proxyFetchEnded = YES;
                            } else if ([type isEqualToString:@"proxy_fetch_error"]) {
                                entry.proxyFetchEnded = YES;
                                entry.proxyFetchError = RequestString(command, @"message", @"WKWEBVIEW_PROXY_FETCH_FAILED");
                            }
                            (void)DeliverProxyFetchCommand(entry, command);
                        }
                        return;
                    }
                    if (![type isEqualToString:@"start_turn"] || requestId.length == 0 || entries[requestId] != nil) {
                        TurnBrokerEmitResult(requestId, TurnBrokerFailure(@"WKWEBVIEW_TURN_BROKER_COMMAND_INVALID", @"", @"broker", 0));
                        return;
                    }
                    NSDictionary *request = [command[@"request"] isKindOfClass:[NSDictionary class]] ? command[@"request"] : nil;
                    if (request == nil) {
                        TurnBrokerEmitResult(requestId, TurnBrokerFailure(@"WKWEBVIEW_TURN_BROKER_INPUT_INVALID", @"", @"broker", 0));
                        return;
                    }
                    BOOL temporary = RequestBool(request, @"minimal_temporary", NO);
                    NSString *lifecycleId = RequestString(request, @"minimal_temporary_lifecycle_id", @"");
                    WKTurnBrokerPage *targetPage = normalPage;
                    if (temporary) {
                        if (lifecycleId.length == 0) {
                            TurnBrokerEmitResult(requestId, TurnBrokerFailure(@"WKWEBVIEW_TEMPORARY_LIFECYCLE_ID_MISSING", @"", @"broker", 0));
                            return;
                        }
                        NSString *conversationId = RequestString(request, @"minimal_conversation_id", @"");
                        WKTurnBrokerPage *page = temporaryPages[lifecycleId];
                        if (page == nil) {
                            if (conversationId.length > 0) {
                                TurnBrokerEmitResult(requestId, TurnBrokerFailure(@"WKWEBVIEW_TEMPORARY_LIFECYCLE_NOT_LIVE", @"", @"broker", 0));
                                return;
                            }
                            page = CreateTurnBrokerPage(
                                router,
                                shellSource,
                                @"https://chatgpt.com/?temporary-chat=true",
                                lifecycleId,
                                NO,
                                YES
                            );
                            if (page == nil) {
                                TurnBrokerEmitResult(requestId, TurnBrokerFailure(@"WKWEBVIEW_TEMPORARY_PAGE_LOAD_TIMEOUT", @"", @"broker", 0));
                                return;
                            }
                            temporaryPages[lifecycleId] = page;
                        } else if (conversationId.length == 0) {
                            TurnBrokerEmitResult(requestId, TurnBrokerFailure(@"WKWEBVIEW_TEMPORARY_LIFECYCLE_ALREADY_BOUND", @"", @"broker", 0));
                            return;
                        }
                        targetPage = page;
                    }
                    WKTurnBrokerEntry *entry = StartTurnBrokerEntry(
                        command,
                        targetPage.webView,
                        targetPage.rootDelegate,
                        router,
                        targetPage.realPage
                    );
                    if (entry == nil) {
                        TurnBrokerEmitResult(requestId, TurnBrokerFailure(@"WKWEBVIEW_TURN_BROKER_INPUT_INVALID", @"", @"broker", 0));
                        return;
                    }
                    entries[requestId] = entry;
                });
            }
        }
    };

    PrintEvent(@{@"type": @"broker_ready"});
    while (!shouldExit) {
        RunLoopFor(0.02);
        for (NSString *requestId in [entries.allKeys copy]) {
            WKTurnBrokerEntry *entry = entries[requestId];
            if (entry == nil) continue;
            NSDictionary *result = nil;
            if (!PumpTurnBrokerEntry(entry, &result)) continue;
            TurnBrokerEmitResult(requestId, result ?: TurnBrokerFailure(@"WKWEBVIEW_TURN_BROKER_RESULT_MISSING", @"", @"broker", 0));
            FinishTurnBrokerEntry(entry, router);
            [entries removeObjectForKey:requestId];
        }
    }
    input.readabilityHandler = nil;
    for (WKTurnBrokerEntry *entry in entries.allValues) FinishTurnBrokerEntry(entry, router);
    for (WKTurnBrokerPage *page in temporaryPages.allValues) CloseTurnBrokerPage(page);
    [temporaryPages removeAllObjects];
    CloseTurnBrokerPage(normalPage);
    return 0;
}

static NSString *CatalogEndpoint(NSString *catalog, NSInteger offset, NSInteger limit, BOOL archived, BOOL starred) {
    if ([catalog isEqualToString:@"models"]) {
        return @"/backend-api/models?history_and_training_disabled=false";
    }
    if (![catalog isEqualToString:@"conversations"]) return nil;
    return [NSString stringWithFormat:
            @"/backend-api/conversations?offset=%ld&limit=%ld&order=updated&is_archived=%@&is_starred=%@",
            (long)MAX(0, offset),
            (long)MAX(1, MIN(100, limit)),
            archived ? @"true" : @"false",
            starred ? @"true" : @"false"];
}

static BOOL ModeMatches(NSDictionary *snapshot, NSString *requested) {
    if (requested.length == 0) return YES;
    NSString *selected = [snapshot[@"selectedMode"] isKindOfClass:[NSString class]] ? snapshot[@"selectedMode"] : nil;
    NSString *wanted = nil;
    if ([requested isEqualToString:@"HIGH"]) wanted = @"High";
    else if ([requested isEqualToString:@"MEDIUM"]) wanted = @"Medium";
    else if ([requested isEqualToString:@"INSTANT"]) wanted = @"Instant";
    return wanted != nil && selected != nil && [selected caseInsensitiveCompare:wanted] == NSOrderedSame;
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        (void)argc;
        (void)argv;
        NSArray<NSString *> *args = [[NSProcessInfo processInfo] arguments];
        if (HasArg(args, @"--turn-broker")) return RunTurnBroker();

        BOOL requestFromStdin = HasArg(args, @"--request-stdin");
        NSDictionary *request = requestFromStdin ? ReadRequestEnvelope() : @{};
        if (request == nil) {
            PrintResult(@{@"ok":@NO,@"error":@"WKWEBVIEW_REQUEST_ENVELOPE_INVALID"});
            return 40;
        }

        NSString *urlString = RequestString(request, @"url", ArgValue(args, @"--url", @"https://chatgpt.com/"));
        NSString *prompt64 = ArgValue(args, @"--prompt-base64", @"");
        NSString *prompt = RequestString(request, @"prompt", DecodeBase64(prompt64));
        NSString *profile = [RequestString(request, @"profile", ArgValue(args, @"--profile", @"")) uppercaseString];
        NSString *minimalConversationId = RequestString(request, @"minimal_conversation_id", ArgValue(args, @"--minimal-conversation-id", @""));
        NSString *minimalParentMessageId = RequestString(request, @"minimal_parent_message_id", ArgValue(args, @"--minimal-parent-message-id", @""));
        NSString *minimalModelSlug = RequestString(request, @"minimal_model_slug", ArgValue(args, @"--minimal-model-slug", @""));
        NSString *minimalThinkingEffort = RequestString(request, @"minimal_thinking_effort", ArgValue(args, @"--minimal-thinking-effort", @""));
        NSString *minimalHandoffAttemptId = RequestString(request, @"minimal_handoff_attempt_id", ArgValue(args, @"--minimal-handoff-attempt-id", @""));
        BOOL minimalTemporary = RequestBool(request, @"minimal_temporary", HasArg(args, @"--minimal-temporary"));
        id requestedMinimalAttachments = request[@"minimal_attachments"];
        NSString *minimalAttachmentsBase64 = [requestedMinimalAttachments isKindOfClass:[NSArray class]]
            ? Base64JSONValue(requestedMinimalAttachments)
            : ArgValue(args, @"--minimal-attachments-base64", @"");
        NSInteger minimalAttachmentCount = [requestedMinimalAttachments isKindOfClass:[NSArray class]]
            ? (NSInteger)[(NSArray *)requestedMinimalAttachments count]
            : RequestInteger(request, @"minimal_attachment_count", [ArgValue(args, @"--minimal-attachment-count", @"0") integerValue]);
        NSString *expectedCurrentNode = RequestString(request, @"expected_current_node", ArgValue(args, @"--expected-current-node", @""));
        NSString *canonicalConversation = RequestString(request, @"canonical_conversation", ArgValue(args, @"--canonical-conversation", @""));
        BOOL canonicalOnly = canonicalConversation.length > 0;
        NSString *resumeConversation = RequestString(request, @"resume_conversation", ArgValue(args, @"--resume-conversation", @""));
        BOOL resumeOnly = resumeConversation.length > 0;
        NSString *legacyResumeValue = ArgValue(args, @"--resume-value", @"");
        NSString *resumeValue = RequestString(request, @"resume_value", legacyResumeValue);
        int resumeHandoffFD = [ArgValue(args, @"--resume-handoff-fd", @"-1") intValue];
        if (resumeHandoffFD >= 0) {
            int flags = fcntl(resumeHandoffFD, F_GETFD);
            if (flags >= 0) (void)fcntl(resumeHandoffFD, F_SETFD, flags | FD_CLOEXEC);
        }
        NSInteger resumeOffset = RequestInteger(request, @"resume_offset", [ArgValue(args, @"--resume-offset", @"0") integerValue]);
        NSString *observeConversation = RequestString(request, @"observe_conversation", ArgValue(args, @"--observe-conversation", @""));
        BOOL observeOnly = observeConversation.length > 0;
        NSString *domObserveConversation = RequestString(request, @"dom_observe_conversation", ArgValue(args, @"--dom-observe-conversation", @""));
        BOOL domObserveOnly = domObserveConversation.length > 0;
        NSTimeInterval observerPollInterval = RequestDouble(request, @"poll_interval", [ArgValue(args, @"--poll-interval", @"1.0") doubleValue]);
        NSString *catalog = [RequestString(request, @"catalog", ArgValue(args, @"--catalog", @"")) lowercaseString];
        BOOL catalogOnly = catalog.length > 0;
        NSInteger catalogOffset = RequestInteger(request, @"offset", [ArgValue(args, @"--offset", @"0") integerValue]);
        NSInteger catalogLimit = RequestInteger(request, @"limit", [ArgValue(args, @"--limit", @"100") integerValue]);
        BOOL catalogArchived = RequestBool(request, @"archived", HasArg(args, @"--archived"));
        BOOL catalogStarred = RequestBool(request, @"starred", HasArg(args, @"--starred"));
        NSArray<NSString *> *attachments = RequestStringArray(request, @"attachments", ArgValues(args, @"--attach"));
        NSTimeInterval timeout = RequestDouble(request, @"timeout", [ArgValue(args, @"--timeout", @"150") doubleValue]);
        BOOL stopOnly = RequestBool(request, @"stop_only", HasArg(args, @"--stop-only"));
        NSString *stopConversation = stopOnly ? ConversationIdFromURL(urlString) : @"";
        BOOL visible = RequestBool(request, @"visible", HasArg(args, @"--visible"));
        BOOL observeSubmit = RequestBool(request, @"observe_submit", HasArg(args, @"--observe-submit"));
        BOOL observeStream = RequestBool(request, @"observe_stream", HasArg(args, @"--observe-stream"));
        BOOL streamObserveUntilEnd = RequestBool(request, @"stream_observe_until_end", HasArg(args, @"--observe-stream-until-end"));
        BOOL streamObserveUntilResumeToken = RequestBool(request, @"stream_observe_until_resume_token", HasArg(args, @"--observe-stream-until-resume-token"));
        BOOL minimalSecurityShell = RequestBool(request, @"minimal_security_shell", HasArg(args, @"--minimal-security-shell"));
        BOOL readOnly = canonicalOnly || catalogOnly || observeOnly || resumeOnly || stopOnly;
        NSInteger operationModeCount = (canonicalOnly ? 1 : 0) + (catalogOnly ? 1 : 0) + (observeOnly ? 1 : 0) + (resumeOnly ? 1 : 0) + (domObserveOnly ? 1 : 0) + (stopOnly ? 1 : 0);
        if (operationModeCount > 1) {
            PrintResult(@{@"ok":@NO,@"error":@"WKWEBVIEW_READ_MODE_CONFLICT"});
            return 18;
        }
        if (catalogOnly && CatalogEndpoint(catalog, catalogOffset, catalogLimit, catalogArchived, catalogStarred) == nil) {
            PrintResult(@{@"ok":@NO,@"error":@"WKWEBVIEW_CATALOG_KIND_UNSUPPORTED"});
            return 19;
        }
        if (resumeOnly && resumeValue.length == 0) {
            PrintResult(@{@"ok":@NO,@"error":@"WKWEBVIEW_RESUME_VALUE_REQUIRED"});
            return 25;
        }
        if (stopOnly && stopConversation.length > 0) {
            urlString = [@"https://chatgpt.com/c/" stringByAppendingString:stopConversation];
        } else if (readOnly) {
            urlString = @"https://chatgpt.com/robots.txt";
        }
        if (domObserveOnly) urlString = [@"https://chatgpt.com/c/" stringByAppendingString:domObserveConversation];
        if (timeout <= 0) timeout = 150;
        if (observerPollInterval <= 0) observerPollInterval = 1.0;
        if (!stopOnly && !readOnly && !domObserveOnly && prompt == nil) {
            PrintResult(@{@"ok":@NO,@"error":@"WKWEBVIEW_PROMPT_BASE64_INVALID"});
            return 2;
        }
        if (minimalSecurityShell && (
            readOnly || domObserveOnly || attachments.count > 0
            || !observeSubmit || !observeStream || !streamObserveUntilResumeToken
        )) {
            PrintResult(@{@"ok":@NO,@"error":@"WKWEBVIEW_MINIMAL_SECURITY_MODE_UNSUPPORTED"});
            return 30;
        }
        if (minimalSecurityShell && ((minimalConversationId.length > 0) != (minimalParentMessageId.length > 0))) {
            PrintResult(@{@"ok":@NO,@"error":@"WKWEBVIEW_MINIMAL_SECURITY_CONTINUATION_IDENTITY_INCOMPLETE"});
            return 35;
        }
        if (minimalSecurityShell && (minimalAttachmentCount < 0 || (minimalAttachmentCount > 0 && minimalAttachmentsBase64.length == 0))) {
            PrintResult(@{@"ok":@NO,@"error":@"WKWEBVIEW_MINIMAL_SECURITY_ATTACHMENT_DESCRIPTOR_INVALID"});
            return 36;
        }

        [NSApplication sharedApplication];
        WKAuthorityDelegate *delegate = [WKAuthorityDelegate new];
        if (minimalSecurityShell && minimalConversationId.length > 0) {
            delegate.streamConversationId = minimalConversationId;
        }
        WKWebViewConfiguration *configuration = [WKWebViewConfiguration new];
        configuration.websiteDataStore = [WKWebsiteDataStore defaultDataStore];
        [configuration.userContentController addScriptMessageHandler:delegate name:@"cwaCanonical"];
        if (observeSubmit) {
            [configuration.userContentController addScriptMessageHandler:delegate name:@"cwaSubmit"];
            WKUserScript *submitObserver = [[WKUserScript alloc] initWithSource:SubmitObservationScript()
                                                                        injectionTime:WKUserScriptInjectionTimeAtDocumentStart
                                                                     forMainFrameOnly:NO];
            [configuration.userContentController addUserScript:submitObserver];
        }
        if (observeStream) {
            [configuration.userContentController addScriptMessageHandler:delegate name:@"cwaStream"];
            WKUserScript *streamObserver = [[WKUserScript alloc] initWithSource:PassiveStreamObservationScript()
                                                                        injectionTime:WKUserScriptInjectionTimeAtDocumentStart
                                                                     forMainFrameOnly:NO];
            [configuration.userContentController addUserScript:streamObserver];
        }
        WKWebView *webView = [[WKWebView alloc] initWithFrame:NSMakeRect(0, 0, 1000, 700)
                                               configuration:configuration];
        NSRect rect = visible ? NSMakeRect(100, 100, 1000, 700) : NSMakeRect(-20000, -20000, 1000, 700);
        NSWindowStyleMask style = visible ? (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable)
                                          : NSWindowStyleMaskBorderless;
        NSWindow *window = [[NSWindow alloc] initWithContentRect:rect styleMask:style backing:NSBackingStoreBuffered defer:NO];
        window.title = @"gptty WKWebView Authority";
        window.contentView = webView;
        [window orderFront:nil];
        if (visible) {
            [NSApp activateIgnoringOtherApps:YES];
            [window makeKeyAndOrderFront:nil];
        }

        delegate.webView = webView;
        delegate.window = window;
        delegate.attachmentPaths = attachments;
        webView.navigationDelegate = delegate;
        webView.UIDelegate = delegate;

        NSTimeInterval started = [NSDate timeIntervalSinceReferenceDate];
        NSURLRequest *initialRequest = [NSURLRequest requestWithURL:[NSURL URLWithString:urlString]];
        if (minimalSecurityShell) {
            [webView loadSimulatedRequest:initialRequest responseHTMLString:MinimalSecurityShellHTML()];
        } else {
            [webView loadRequest:initialRequest];
        }
        NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:timeout];
        NSDictionary *readySnapshot = nil;

        if (minimalSecurityShell) {
            while (!delegate.navigationFinished && [deadline timeIntervalSinceNow] > 0) RunLoopFor(0.05);
            if (!delegate.navigationFinished) {
                PrintResult(@{@"ok":@NO,@"error":@"WKWEBVIEW_MINIMAL_SECURITY_LOAD_TIMEOUT"});
                return 31;
            }
            NSString *minimalScript = MinimalSecurityWriteScript(
                prompt,
                profile,
                minimalConversationId,
                minimalParentMessageId,
                minimalModelSlug,
                minimalThinkingEffort,
                minimalAttachmentsBase64,
                minimalHandoffAttemptId,
                minimalTemporary
            );
            if (minimalScript.length == 0) {
                PrintResult(@{@"ok":@NO,@"error":@"WKWEBVIEW_MINIMAL_SECURITY_SCRIPT_MISSING"});
                return 32;
            }
            delegate.canonicalDone = NO;
            delegate.canonicalResult = nil;
            EvaluateSync(webView, minimalScript, 2.0, nil);
            BOOL identityPrinted = NO;
            NSDate *identityRecoveryDeadline = nil;
            while ([deadline timeIntervalSinceNow] > 0) {
                RunLoopFor(0.05);
                NSDictionary *launch = delegate.canonicalResult;
                if (delegate.canonicalDone && [launch isKindOfClass:[NSDictionary class]] && ![launch[@"ok"] boolValue]) {
                    PrintResult(@{
                        @"ok":@NO,
                        @"error":@"WKWEBVIEW_MINIMAL_SECURITY_WRITE_FAILED",
                        @"detail":[launch[@"error"] isKindOfClass:[NSString class]] ? launch[@"error"] : @"",
                        @"stage":[launch[@"stage"] isKindOfClass:[NSString class]] ? launch[@"stage"] : @"",
                        @"status":[launch[@"status"] isKindOfClass:[NSNumber class]] ? launch[@"status"] : @0
                    });
                    return 33;
                }
                if (!identityPrinted && delegate.streamConversationId.length > 0) {
                    PrintEvent(@{
                        @"type":@"write_identity_resolved",
                        @"conversation_id":delegate.streamConversationId,
                        @"submit_response_observed":@(delegate.submitResponseObserved),
                        @"submit_response_status":@(delegate.submitStatus)
                    });
                    identityPrinted = YES;
                }
                BOOL responseOK = (delegate.streamResponseObserved && delegate.streamStatus >= 200 && delegate.streamStatus < 300)
                    || (delegate.submitResponseObserved && delegate.submitStatus >= 200 && delegate.submitStatus < 300);
                if (
                    identityRecoveryDeadline == nil
                    && responseOK
                    && (
                        minimalConversationId.length > 0
                        || delegate.streamClientMessageId.length > 0
                    )
                ) {
                    identityRecoveryDeadline = [NSDate dateWithTimeIntervalSinceNow:8.0];
                }
                BOOL terminalCompletionFence = responseOK
                    && minimalConversationId.length > 0
                    && delegate.streamTerminalObserved
                    && delegate.streamConversationId.length > 0;
                BOOL topicHandoffFence = responseOK
                    && delegate.streamTopicId.length > 0
                    && delegate.streamConversationId.length > 0
                    && (
                        minimalConversationId.length == 0
                        || [delegate.streamConversationId isEqualToString:minimalConversationId]
                    );
                if (terminalCompletionFence || topicHandoffFence) break;
                if (responseOK && delegate.streamResumeToken.length > 0 && delegate.streamConversationId.length > 0) break;
                if (identityRecoveryDeadline != nil && [identityRecoveryDeadline timeIntervalSinceNow] <= 0) break;
            }
            BOOL responseOK = (delegate.streamResponseObserved && delegate.streamStatus >= 200 && delegate.streamStatus < 300)
                || (delegate.submitResponseObserved && delegate.submitStatus >= 200 && delegate.submitStatus < 300);
            BOOL terminalCompletionFence = responseOK
                && minimalConversationId.length > 0
                && delegate.streamTerminalObserved
                && delegate.streamConversationId.length > 0;
            BOOL topicHandoffFence = responseOK
                && delegate.streamTopicId.length > 0
                && delegate.streamConversationId.length > 0
                && (
                    minimalConversationId.length == 0
                    || [delegate.streamConversationId isEqualToString:minimalConversationId]
                );
            BOOL resumeFenceObserved = responseOK
                && delegate.streamResumeToken.length > 0
                && delegate.streamConversationId.length > 0;
            BOOL identityRecoveryRequired = !terminalCompletionFence
                && !topicHandoffFence
                && !resumeFenceObserved
                && (
                    minimalConversationId.length > 0
                    || (responseOK && delegate.streamClientMessageId.length > 0)
                );
            if (identityRecoveryRequired) {
                BOOL recoveryHandoffWritten = NO;
                if (resumeHandoffFD >= 0) {
                    NSString *privateHandoff = PrivateTurnHandoffJSON(delegate);
                    recoveryHandoffWritten = WriteUTF8ToFD(privateHandoff, resumeHandoffFD);
                    close(resumeHandoffFD);
                    resumeHandoffFD = -1;
                }
                PrintResult(@{
                    @"ok":@YES,
                    @"identity_recovery_required":@YES,
                    @"client_message_id":delegate.streamClientMessageId ?: @"",
                    @"conversation_id":delegate.streamConversationId.length > 0
                        ? delegate.streamConversationId
                        : (minimalConversationId ?: @""),
                    @"response_status":@(delegate.streamResponseObserved ? delegate.streamStatus : delegate.submitStatus),
                    @"submit_response_status":@(delegate.submitStatus),
                    @"stream_response_status":@(delegate.streamStatus),
                    @"write_commit_proven":@NO,
                    @"canonical_committed":@NO,
                    @"stream_terminal_observed":@(delegate.streamTerminalObserved),
                    @"stream_resume_present":@NO,
                    @"stream_resume_handoff_written":@NO,
                    @"private_turn_context_handoff_written":@(recoveryHandoffWritten),
                    @"stream_handoff_observed":@(delegate.streamHandoffObserved),
                    @"stream_topic_id":delegate.streamTopicId ?: @"",
                    @"turn_exchange_id":delegate.streamTurnExchangeId ?: @"",
                    @"attachment_count":@(MAX(0, minimalAttachmentCount)),
                    @"minimal_security_shell":@YES
                });
                return 0;
            }
            if (!terminalCompletionFence && !topicHandoffFence && !resumeFenceObserved) {
                PrintResult(@{
                    @"ok":@NO,
                    @"error":@"WKWEBVIEW_MINIMAL_SECURITY_STREAM_FENCE_MISSING",
                    @"submit_response_status":@(delegate.submitStatus),
                    @"stream_response_status":@(delegate.streamStatus),
                    @"resume_token_present":@(delegate.streamResumeToken.length > 0),
                    @"stream_topic_present":@(delegate.streamTopicId.length > 0),
                    @"conversation_id_present":@(delegate.streamConversationId.length > 0)
                });
                return 34;
            }
            NSString *writeCommitProof = resumeFenceObserved
                ? @"RESUME_FENCE"
                : (topicHandoffFence ? @"TOPIC_HANDOFF_FENCE" : @"PHASE_A_TERMINAL");
            BOOL resumeHandoffWritten = NO;
            if (resumeHandoffFD >= 0) {
                NSString *privateHandoff = PrivateTurnHandoffJSON(delegate);
                resumeHandoffWritten = WriteUTF8ToFD(privateHandoff, resumeHandoffFD);
                close(resumeHandoffFD);
                resumeHandoffFD = -1;
            }
            NSTimeInterval elapsed = [NSDate timeIntervalSinceReferenceDate] - started;
            NSTimeInterval loadElapsed = delegate.navigationFinishedAt > 0 ? delegate.navigationFinishedAt - started : 0;
            PrintResult(@{
                @"ok":@YES,
                @"conversation_id":delegate.streamConversationId,
                @"response_status":@(delegate.streamResponseObserved ? delegate.streamStatus : delegate.submitStatus),
                @"submit_request_observed":@(delegate.submitRequestObserved),
                @"submit_temporary_mode_observed":@(delegate.submitTemporaryModeObserved),
                @"submit_response_observed":@(delegate.submitResponseObserved),
                @"submit_response_status":@(delegate.submitStatus),
                @"stream_response_observed":@(delegate.streamResponseObserved),
                @"stream_response_status":@(delegate.streamStatus),
                @"elapsed_ms":@((NSInteger)llround(elapsed * 1000.0)),
                @"load_elapsed_ms":@((NSInteger)llround(MAX(0, loadElapsed) * 1000.0)),
                @"attachment_count":@(MAX(0, minimalAttachmentCount)),
                @"profile":profile ?: @"",
                @"write_commit_proven":@YES,
                @"write_commit_proof":writeCommitProof,
                @"canonical_committed":@NO,
                @"canonical_final_completed":@NO,
                @"canonical_body_base64":@"",
                @"committed_current_node":@"",
                @"stream_started":@(delegate.streamStarted),
                @"stream_ended":@(delegate.streamEnded),
                @"stream_terminal_observed":@(delegate.streamTerminalObserved),
                @"stream_resume_present":@(resumeFenceObserved),
                @"stream_resume_handoff_written":@(resumeFenceObserved && resumeHandoffWritten),
                @"stream_handoff_observed":@(delegate.streamHandoffObserved),
                @"stream_topic_id":delegate.streamTopicId ?: @"",
                @"turn_exchange_id":delegate.streamTurnExchangeId ?: @"",
                @"stream_conversation_id":delegate.streamConversationId,
                @"minimal_security_shell":@YES,
                @"bundle_id":@"local.gptty.webkit-authority"
            });
            return 0;
        }

        if (readOnly) {
            while (!delegate.navigationFinished && [deadline timeIntervalSinceNow] > 0) {
                RunLoopFor(0.1);
            }
            if (!delegate.navigationFinished) {
                PrintResult(@{@"ok":@NO,@"error":@"WKWEBVIEW_CANONICAL_ORIGIN_NOT_READY"});
                return 15;
            }
            if (stopOnly) {
                if (stopConversation.length == 0) {
                    PrintResult(@{@"ok":@NO,@"error":@"WKWEBVIEW_STOP_CONVERSATION_ID_UNRESOLVED"});
                    return 28;
                }
                NSDate *stopControlDeadline = [NSDate dateWithTimeIntervalSinceNow:MIN(12.0, MAX(0.1, [deadline timeIntervalSinceNow]))];
                NSDictionary *stopResult = nil;
                while ([stopControlDeadline timeIntervalSinceNow] > 0) {
                    stopResult = ParseJSONResult(EvaluateSync(webView, StopScript(), 0.5, nil));
                    if ([stopResult[@"ok"] boolValue]) break;
                    RunLoopFor(0.1);
                }
                if (![stopResult[@"ok"] boolValue]) {
                    PrintResult(@{
                        @"ok":@NO,
                        @"error":@"WKWEBVIEW_STOP_CONTROL_NOT_FOUND",
                        @"conversation_id":stopConversation,
                        @"detail": stopResult[@"reason"] ?: @"no_stop"
                    });
                    return 29;
                }
                PrintResult(@{
                    @"ok":@YES,
                    @"stop_requested":@YES,
                    @"stop_control_clicked":@YES,
                    @"conversation_id":stopConversation
                });
                return 0;
            }
            if (resumeOnly) {
                delegate.canonicalDone = NO;
                delegate.canonicalResult = nil;
                EvaluateSync(
                    webView,
                    ResumeConversationScript(resumeConversation, resumeValue, resumeOffset),
                    2.0,
                    nil
                );
                NSDate *resumeStartDeadline = [NSDate dateWithTimeIntervalSinceNow:MIN(8.0, MAX(0.1, [deadline timeIntervalSinceNow]))];
                while (!delegate.canonicalDone && [resumeStartDeadline timeIntervalSinceNow] > 0) {
                    RunLoopFor(0.05);
                }
                NSDictionary *resumeResult = delegate.canonicalResult;
                BOOL resumeOK = [resumeResult isKindOfClass:[NSDictionary class]] && [resumeResult[@"ok"] boolValue];
                NSNumber *resumeStatus = [resumeResult[@"status"] isKindOfClass:[NSNumber class]] ? resumeResult[@"status"] : @0;
                if (!resumeOK) {
                    PrintResult(@{
                        @"ok":@NO,
                        @"error":@"WKWEBVIEW_RESUME_HTTP_FAILED",
                        @"status":resumeStatus,
                        @"conversation_id":resumeConversation,
                        @"offset":@(resumeOffset)
                    });
                    return 26;
                }
                BOOL canonicalCompleted = NO;
                NSString *canonicalBody = @"";
                NSTimeInterval canonicalPollDelay = 1.0;
                NSTimeInterval nextCanonicalCheckAt = [NSDate timeIntervalSinceReferenceDate];
                while (!canonicalCompleted && [deadline timeIntervalSinceNow] > 0) {
                    RunLoopFor(0.05);
                    NSTimeInterval now = [NSDate timeIntervalSinceReferenceDate];
                    if (now < nextCanonicalCheckAt) continue;
                    delegate.canonicalDone = NO;
                    delegate.canonicalResult = nil;
                    EvaluateSync(webView, CanonicalCompletionCheckScript(resumeConversation), 1.5, nil);
                    NSDate *canonicalDeadline = [NSDate dateWithTimeIntervalSinceNow:MIN(2.0, MAX(0.05, [deadline timeIntervalSinceNow]))];
                    while (!delegate.canonicalDone && [canonicalDeadline timeIntervalSinceNow] > 0) {
                        RunLoopFor(0.025);
                    }
                    NSDictionary *completionResult = delegate.canonicalResult;
                    canonicalCompleted = [completionResult isKindOfClass:[NSDictionary class]] && [completionResult[@"completed"] boolValue];
                    if (canonicalCompleted && [completionResult[@"body"] isKindOfClass:[NSString class]]) {
                        canonicalBody = completionResult[@"body"];
                    }
                    NSNumber *completionStatus = [completionResult[@"status"] isKindOfClass:[NSNumber class]] ? completionResult[@"status"] : @0;
                    NSTimeInterval completionDelay = completionStatus.integerValue == 429
                        ? MAX(canonicalPollDelay, 60.0)
                        : canonicalPollDelay;
                    nextCanonicalCheckAt = [NSDate timeIntervalSinceReferenceDate] + completionDelay;
                    if (completionStatus.integerValue != 429) canonicalPollDelay = MIN(canonicalPollDelay * 2.0, 8.0);
                }
                NSData *canonicalBodyData = [canonicalBody dataUsingEncoding:NSUTF8StringEncoding] ?: [NSData data];
                NSString *canonicalBodyBase64 = [canonicalBodyData base64EncodedStringWithOptions:0] ?: @"";
                PrintResult(@{
                    @"ok":@(canonicalCompleted),
                    @"status":resumeStatus,
                    @"conversation_id":resumeConversation,
                    @"offset":@(resumeOffset),
                    @"stream_started":@(delegate.streamStarted),
                    @"stream_ended":@(delegate.streamEnded),
                    @"stream_terminal_observed":@(delegate.streamTerminalObserved),
                    @"canonical_completed":@(canonicalCompleted),
                    @"canonical_body_base64":canonicalBodyBase64
                });
                return canonicalCompleted ? 0 : 27;
            }
            if (observeOnly) {
                while ([deadline timeIntervalSinceNow] > 0) {
                    delegate.canonicalDone = NO;
                    delegate.canonicalResult = nil;
                    EvaluateSync(webView, CanonicalFetchScript(observeConversation), 2.0, nil);
                    NSDate *readDeadline = [NSDate dateWithTimeIntervalSinceNow:MIN(4.0, MAX(0.1, [deadline timeIntervalSinceNow]))];
                    while (!delegate.canonicalDone && [readDeadline timeIntervalSinceNow] > 0) {
                        RunLoopFor(0.05);
                    }
                    NSDictionary *canonical = delegate.canonicalResult;
                    if ([canonical isKindOfClass:[NSDictionary class]]) {
                        NSString *body = [canonical[@"body"] isKindOfClass:[NSString class]] ? canonical[@"body"] : @"";
                        NSData *bodyData = [body dataUsingEncoding:NSUTF8StringEncoding] ?: [NSData data];
                        NSString *bodyBase64 = [bodyData base64EncodedStringWithOptions:0] ?: @"";
                        NSNumber *status = [canonical[@"status"] isKindOfClass:[NSNumber class]] ? canonical[@"status"] : @0;
                        PrintEvent(@{
                            @"type":@"canonical_payload",
                            @"conversation_id":observeConversation,
                            @"ok":@([canonical[@"ok"] boolValue]),
                            @"status":status,
                            @"body_base64":bodyBase64,
                            @"error":[canonical[@"error"] isKindOfClass:[NSString class]] ? canonical[@"error"] : @""
                        });
                    }
                    NSTimeInterval remaining = [deadline timeIntervalSinceNow];
                    if (remaining <= 0) break;
                    NSNumber *observerStatus = [canonical[@"status"] isKindOfClass:[NSNumber class]] ? canonical[@"status"] : @0;
                    NSTimeInterval observerDelay = observerStatus.integerValue == 429
                        ? MAX(observerPollInterval, 60.0)
                        : observerPollInterval;
                    RunLoopFor(MIN(observerDelay, remaining));
                }
                PrintResult(@{@"ok":@NO,@"error":@"WKWEBVIEW_CANONICAL_OBSERVER_TIMEOUT",@"conversation_id":observeConversation});
                return 22;
            }
            delegate.canonicalDone = NO;
            delegate.canonicalResult = nil;
            NSString *readScript = canonicalOnly
                ? CanonicalFetchScript(canonicalConversation)
                : AuthenticatedFetchScript(CatalogEndpoint(catalog, catalogOffset, catalogLimit, catalogArchived, catalogStarred));
            EvaluateSync(webView, readScript, 2.0, nil);
            while (!delegate.canonicalDone && [deadline timeIntervalSinceNow] > 0) {
                RunLoopFor(0.1);
            }
            NSDictionary *canonical = delegate.canonicalResult;
            if (!delegate.canonicalDone || ![canonical isKindOfClass:[NSDictionary class]]) {
                PrintResult(@{@"ok":@NO,@"error":@"WKWEBVIEW_CANONICAL_READ_TIMEOUT"});
                return 16;
            }
            NSString *body = [canonical[@"body"] isKindOfClass:[NSString class]] ? canonical[@"body"] : @"";
            NSData *bodyData = [body dataUsingEncoding:NSUTF8StringEncoding] ?: [NSData data];
            NSString *bodyBase64 = [bodyData base64EncodedStringWithOptions:0] ?: @"";
            BOOL ok = [canonical[@"ok"] boolValue];
            NSNumber *status = [canonical[@"status"] isKindOfClass:[NSNumber class]] ? canonical[@"status"] : @0;
            NSString *contentType = [canonical[@"contentType"] isKindOfClass:[NSString class]] ? canonical[@"contentType"] : @"";
            NSMutableDictionary *result = [@{
                @"ok":@(ok),
                @"status":status,
                @"content_type":contentType,
                @"body_base64":bodyBase64
            } mutableCopy];
            if (canonicalOnly) result[@"conversation_id"] = canonicalConversation;
            if (catalogOnly) {
                result[@"catalog"] = catalog;
                result[@"offset"] = @(catalogOffset);
                result[@"limit"] = @(catalogLimit);
                result[@"is_archived"] = @(catalogArchived);
                result[@"is_starred"] = @(catalogStarred);
            }
            if (!ok) {
                result[@"error"] = [canonical[@"error"] isKindOfClass:[NSString class]] ? canonical[@"error"] : @"WKWEBVIEW_CANONICAL_HTTP_FAILED";
            }
            PrintResult(result);
            return ok ? 0 : 17;
        }

        while ([deadline timeIntervalSinceNow] > 0) {
            RunLoopFor(0.15);
            NSError *error = nil;
            id raw = EvaluateSync(webView, ReadinessScript(), 1.5, &error);
            NSDictionary *snapshot = ParseJSONResult(raw);
            if (snapshot) {
                readySnapshot = snapshot;
                if ([snapshot[@"login"] boolValue]) {
                    PrintResult(@{@"ok":@NO,@"error":@"WKWEBVIEW_AUTHORITY_LOGIN_REQUIRED",@"final_url":snapshot[@"url"] ?: @""});
                    return 3;
                }
                if (stopOnly) {
                    if ([snapshot[@"stop"] boolValue]) break;
                } else if (domObserveOnly) {
                    NSString *latestMessageId = [snapshot[@"latestMessageId"] isKindOfClass:[NSString class]] ? snapshot[@"latestMessageId"] : nil;
                    if (latestMessageId.length > 0 || [snapshot[@"stop"] boolValue]) break;
                } else {
                    BOOL parentReady = YES;
                    if (expectedCurrentNode.length > 0) {
                        NSString *latestMessageId = [snapshot[@"latestMessageId"] isKindOfClass:[NSString class]] ? snapshot[@"latestMessageId"] : nil;
                        parentReady = latestMessageId != nil && [latestMessageId isEqualToString:expectedCurrentNode];
                    }
                    if ([snapshot[@"composerReady"] boolValue]
                        && parentReady
                        && (profile.length == 0 || ModeMatches(snapshot, profile))) {
                        break;
                    }
                }
            }
            EvaluateSync(webView, ScrollBottomScript(), 0.5, nil);
        }

        if (domObserveOnly) {
            BOOL sawStop = NO;
            BOOL lastStop = NO;
            NSString *lastText = nil;
            NSString *lastMessageId = nil;
            while ([deadline timeIntervalSinceNow] > 0) {
                NSDictionary *snapshot = ParseJSONResult(EvaluateSync(webView, DOMStreamSnapshotScript(), 1.0, nil));
                if (snapshot) {
                    BOOL stop = [snapshot[@"stop"] boolValue];
                    NSString *text = [snapshot[@"text"] isKindOfClass:[NSString class]] ? snapshot[@"text"] : @"";
                    NSString *messageId = [snapshot[@"messageId"] isKindOfClass:[NSString class]] ? snapshot[@"messageId"] : @"";
                    BOOL changed = lastText == nil || ![lastText isEqualToString:text] || lastMessageId == nil || ![lastMessageId isEqualToString:messageId] || stop != lastStop;
                    if (changed) {
                        PrintEvent(@{
                            @"type":@"dom_stream_snapshot",
                            @"conversation_id":domObserveConversation,
                            @"message_id":messageId,
                            @"text":text,
                            @"text_length":@(text.length),
                            @"stop":@(stop),
                            @"assistant_count":[snapshot[@"assistantCount"] isKindOfClass:[NSNumber class]] ? snapshot[@"assistantCount"] : @0
                        });
                        lastText = text;
                        lastMessageId = messageId;
                        lastStop = stop;
                    }
                    if (stop) sawStop = YES;
                    if (sawStop && !stop && text.length > 0) {
                        PrintResult(@{@"ok":@YES,@"conversation_id":domObserveConversation,@"message_id":messageId,@"text_length":@(text.length)});
                        return 0;
                    }
                }
                RunLoopFor(MIN(0.25, MAX(0.0, [deadline timeIntervalSinceNow])));
            }
            PrintResult(@{@"ok":@NO,@"error":@"WKWEBVIEW_DOM_OBSERVER_TIMEOUT",@"conversation_id":domObserveConversation});
            return 23;
        }

        if (stopOnly) {
            if (![readySnapshot[@"stop"] boolValue]) {
                PrintResult(@{@"ok":@NO,@"error":@"WKWEBVIEW_STOP_CONTROL_NOT_FOUND",@"final_url":webView.URL.absoluteString ?: @""});
                return 4;
            }
            NSDictionary *clicked = ParseJSONResult(EvaluateSync(webView, StopScript(), 2.0, nil));
            if (![clicked[@"ok"] boolValue]) {
                PrintResult(@{@"ok":@NO,@"error":@"WKWEBVIEW_STOP_CLICK_FAILED"});
                return 5;
            }
            NSDate *stopDeadline = [NSDate dateWithTimeIntervalSinceNow:MIN(8.0, timeout)];
            BOOL stopped = NO;
            while ([stopDeadline timeIntervalSinceNow] > 0) {
                RunLoopFor(0.2);
                NSDictionary *snapshot = ParseJSONResult(EvaluateSync(webView, ReadinessScript(), 1.0, nil));
                if (snapshot && ![snapshot[@"stop"] boolValue]) { stopped = YES; break; }
            }
            PrintResult(@{@"ok":@YES,@"stopped":@(stopped),@"final_url":webView.URL.absoluteString ?: @""});
            return stopped ? 0 : 6;
        }

        if (![readySnapshot[@"composerReady"] boolValue]) {
            PrintResult(@{@"ok":@NO,@"error":@"WKWEBVIEW_COMPOSER_NOT_READY",@"final_url":webView.URL.absoluteString ?: @""});
            return 7;
        }
        if (expectedCurrentNode.length > 0) {
            NSString *latestMessageId = [readySnapshot[@"latestMessageId"] isKindOfClass:[NSString class]] ? readySnapshot[@"latestMessageId"] : nil;
            if (latestMessageId == nil || ![latestMessageId isEqualToString:expectedCurrentNode]) {
                NSString *detail = [NSString stringWithFormat:@"WKWEBVIEW_PARENT_NOT_HYDRATED:expected=%@:observed=%@", expectedCurrentNode, latestMessageId ?: @""];
                PrintResult(@{@"ok":@NO,@"error":detail,@"final_url":webView.URL.absoluteString ?: @""});
                return 20;
            }
        }
        if (profile.length > 0 && !ModeMatches(readySnapshot, profile)) {
            NSString *detail = [NSString stringWithFormat:@"WKWEBVIEW_PROFILE_NOT_SELECTED:%@:selected=%@:latest_message_id=%@:composer_tag=%@:contenteditable=%@:class=%@:candidates=%@", profile, readySnapshot[@"selectedMode"] ?: @"", readySnapshot[@"latestMessageId"] ?: @"", readySnapshot[@"composerTag"] ?: @"", readySnapshot[@"composerContentEditable"] ?: @"", readySnapshot[@"composerClass"] ?: @"", readySnapshot[@"modeCandidates"] ?: @[]];
            PrintResult(@{@"ok":@NO,@"error":detail,@"final_url":webView.URL.absoluteString ?: @""});
            return 8;
        }
        PrintEvent(@{@"type":@"composer_ready"});

        NSInteger baselineAssistantCount = [[EvaluateSync(webView, AssistantCountScript(), 1.0, nil) description] integerValue];
        if (attachments.count > 0) {
            NSDictionary *clicked = ParseJSONResult(EvaluateSync(webView, ClickFileScript(), 2.0, nil));
            if (![clicked[@"ok"] boolValue]) {
                PrintResult(@{@"ok":@NO,@"error":@"WKWEBVIEW_FILE_INPUT_NOT_READY"});
                return 9;
            }
            RunLoopFor(2.0);
            if (delegate.chooserCount == 0) {
                NSString *injectScript = InjectAttachmentFilesScript(attachments);
                NSDictionary *injected = injectScript.length > 0
                    ? ParseJSONResult(EvaluateSync(webView, injectScript, 8.0, nil))
                    : nil;
                NSInteger injectedCount = [injected[@"count"] respondsToSelector:@selector(integerValue)] ? [injected[@"count"] integerValue] : 0;
                if (![injected[@"ok"] boolValue] || injectedCount != (NSInteger)attachments.count) {
                    NSString *reason = [injected[@"reason"] isKindOfClass:[NSString class]] ? injected[@"reason"] : @"count_mismatch";
                    PrintResult(@{@"ok":@NO,@"error":@"WKWEBVIEW_FILE_INJECTION_FAILED",@"detail":reason});
                    return 10;
                }
                RunLoopFor(2.0);
            }
        }

        NSDictionary *filled = ParseJSONResult(EvaluateSync(webView, FillScript(prompt), 2.0, nil));
        if (![filled[@"ok"] boolValue]) {
            PrintResult(@{@"ok":@NO,@"error":@"WKWEBVIEW_COMPOSER_FILL_FAILED"});
            return 11;
        }
        PrintEvent(@{@"type":@"composer_filled"});
        NSDate *sendReadyDeadline = [NSDate dateWithTimeIntervalSinceNow:MIN(5.0, MAX(0.1, [deadline timeIntervalSinceNow]))];
        BOOL sendReady = NO;
        NSDictionary *lastSendSnapshot = nil;
        while ([sendReadyDeadline timeIntervalSinceNow] > 0) {
            NSDictionary *snapshot = ParseJSONResult(EvaluateSync(webView, ReadinessScript(), 1.0, nil));
            if (snapshot) lastSendSnapshot = snapshot;
            if (snapshot && [snapshot[@"send"] boolValue]) {
                sendReady = YES;
                break;
            }
            RunLoopFor(0.1);
        }
        if (!sendReady) {
            NSString *bodyTail = [lastSendSnapshot[@"bodyTail"] isKindOfClass:[NSString class]] ? lastSendSnapshot[@"bodyTail"] : @"";
            NSString *bodyTailLower = bodyTail.lowercaseString;
            BOOL rateLimited = [bodyTailLower containsString:@"too many requests"]
                && [bodyTailLower containsString:@"temporarily limited access to your conversations"];
            PrintResult(@{
                @"ok":@NO,
                @"error":rateLimited ? @"WKWEBVIEW_CHATGPT_RATE_LIMITED" : @"WKWEBVIEW_SEND_CONTROL_NOT_READY"
            });
            return 12;
        }
        PrintEvent(@{@"type":@"send_ready"});
        BOOL sendClicked = NO;
        NSDate *sendClickDeadline = [NSDate dateWithTimeIntervalSinceNow:MIN(5.0, MAX(0.1, [deadline timeIntervalSinceNow]))];
        while (!sendClicked && [sendClickDeadline timeIntervalSinceNow] > 0) {
            if (delegate.submitRequestObserved) {
                sendClicked = YES;
                break;
            }
            NSDictionary *sent = NativeClickSendButton(webView);
            if ([sent[@"ok"] boolValue]) {
                sendClicked = YES;
                break;
            }
            RunLoopFor(0.1);
        }
        if (!sendClicked) {
            PrintResult(@{@"ok":@NO,@"error":@"WKWEBVIEW_SEND_CONTROL_CLICK_FAILED"});
            return 12;
        }
        PrintEvent(@{@"type":@"send_action_completed"});

        NSString *inputConversationId = ConversationIdFromURL(urlString);
        NSString *resolvedConversationId = inputConversationId;
        NSDictionary *accepted = nil;
        while ([deadline timeIntervalSinceNow] > 0) {
            RunLoopFor(0.2);
            NSDictionary *snapshot = ParseJSONResult(EvaluateSync(webView, AcceptanceScript(prompt, baselineAssistantCount), 1.0, nil));
            if (!snapshot) continue;
            if ([snapshot[@"error"] boolValue]) {
                PrintResult(@{@"ok":@NO,@"error":@"WKWEBVIEW_CHATGPT_UI_ERROR",@"detail":snapshot[@"bodyTail"] ?: @""});
                return 13;
            }
            NSString *routeConversationId = ConversationIdFromURL(snapshot[@"url"]);
            if (routeConversationId.length > 0) resolvedConversationId = routeConversationId;
            BOOL productActivity = [snapshot[@"stop"] boolValue] || [snapshot[@"newAssistant"] boolValue];
            if ([snapshot[@"userSeen"] boolValue] && productActivity && resolvedConversationId.length > 0) {
                accepted = snapshot;
                break;
            }
        }

        if (!accepted || resolvedConversationId.length == 0) {
            PrintResult(@{
                @"ok":@NO,
                @"error":@"WKWEBVIEW_WRITE_ACCEPTANCE_NOT_PROVEN",
                @"final_url":webView.URL.absoluteString ?: @"",
                @"submit_request_observed":@(delegate.submitRequestObserved),
                @"submit_response_observed":@(delegate.submitResponseObserved),
                @"submit_response_status":@(delegate.submitStatus),
                @"submit_error":delegate.submitError ?: @""
            });
            return 14;
        }

        PrintEvent(@{
            @"type":@"write_identity_resolved",
            @"conversation_id":resolvedConversationId,
            @"submit_response_observed":@(delegate.submitResponseObserved),
            @"submit_response_status":@(delegate.submitStatus)
        });

        if (
            observeStream
            && streamObserveUntilResumeToken
            && delegate.streamResumeToken.length == 0
            && delegate.streamTopicId.length == 0
            && !delegate.streamEnded
        ) {
            NSDate *resumeTokenDeadline = deadline;
            while (
                delegate.streamResumeToken.length == 0
                && delegate.streamTopicId.length == 0
                && !delegate.streamEnded
                && !delegate.streamTerminalObserved
                && [resumeTokenDeadline timeIntervalSinceNow] > 0
            ) {
                RunLoopFor(0.05);
            }
        }

        BOOL submitObserverSucceeded = delegate.submitResponseObserved
            && delegate.submitStatus >= 200
            && delegate.submitStatus < 300;
        BOOL streamResponseSucceeded = delegate.streamResponseObserved
            && delegate.streamStatus >= 200
            && delegate.streamStatus < 300;
        BOOL submitSucceeded = submitObserverSucceeded || streamResponseSucceeded;
        BOOL resumeConversationMatches = delegate.streamConversationId.length > 0
            && [delegate.streamConversationId isEqualToString:resolvedConversationId];
        BOOL resumeCommitFence = observeStream
            && streamObserveUntilResumeToken
            && accepted != nil
            && submitSucceeded
            && delegate.streamResumeToken.length > 0
            && resumeConversationMatches;
        BOOL topicHandoffCommitFence = observeStream
            && streamObserveUntilResumeToken
            && accepted != nil
            && submitSucceeded
            && delegate.streamTopicId.length > 0
            && resumeConversationMatches;

        BOOL canonicalCommitted = NO;
        NSString *committedCurrentNode = @"";
        BOOL streamingResumeMode = observeStream && streamObserveUntilResumeToken;
        BOOL streamTerminalCommitFence = streamingResumeMode
            && accepted != nil
            && submitSucceeded
            && delegate.streamTerminalObserved;

        if (!streamingResumeMode && !resumeCommitFence) {
            NSDate *commitDeadline = [NSDate dateWithTimeIntervalSinceNow:MIN(30.0, MAX(1.0, [deadline timeIntervalSinceNow]))];
            NSTimeInterval commitPollDelay = 1.0;
            while ([commitDeadline timeIntervalSinceNow] > 0) {
                delegate.canonicalDone = NO;
                delegate.canonicalResult = nil;
                EvaluateSync(
                    webView,
                    CanonicalCommitCheckScript(resolvedConversationId, prompt, expectedCurrentNode),
                    2.0,
                    nil
                );
                NSDate *readDeadline = [NSDate dateWithTimeIntervalSinceNow:MIN(3.0, MAX(0.1, [commitDeadline timeIntervalSinceNow]))];
                while (!delegate.canonicalDone && [readDeadline timeIntervalSinceNow] > 0) RunLoopFor(0.05);
                NSDictionary *proof = delegate.canonicalResult;
                if ([proof isKindOfClass:[NSDictionary class]] && [proof[@"committed"] boolValue]) {
                    canonicalCommitted = YES;
                    committedCurrentNode = [proof[@"currentNode"] isKindOfClass:[NSString class]] ? proof[@"currentNode"] : @"";
                    break;
                }
                NSNumber *proofStatus = [proof[@"status"] isKindOfClass:[NSNumber class]] ? proof[@"status"] : @0;
                NSTimeInterval proofDelay = proofStatus.integerValue == 429
                    ? MAX(commitPollDelay, 60.0)
                    : commitPollDelay;
                NSTimeInterval commitRemaining = [commitDeadline timeIntervalSinceNow];
                if (commitRemaining > 0) RunLoopFor(MIN(proofDelay, commitRemaining));
                if (proofStatus.integerValue != 429) commitPollDelay = MIN(commitPollDelay * 2.0, 8.0);
            }
        }

        BOOL writeCommitProven = canonicalCommitted || resumeCommitFence || topicHandoffCommitFence || streamTerminalCommitFence;
        if (!writeCommitProven) {
            PrintResult(@{
                @"ok":@NO,
                @"error":@"WKWEBVIEW_WRITE_CANONICAL_COMMIT_NOT_PROVEN",
                @"conversation_id":resolvedConversationId,
                @"final_url":webView.URL.absoluteString ?: @"",
                @"submit_response_observed":@(delegate.submitResponseObserved),
                @"submit_response_status":@(delegate.submitStatus),
                @"resume_token_present":@(delegate.streamResumeToken.length > 0),
                @"resume_conversation_matches":@(resumeConversationMatches),
                @"stream_terminal_observed":@(delegate.streamTerminalObserved)
            });
            return 21;
        }

        if (
            observeStream
            && streamObserveUntilEnd
            && !streamObserveUntilResumeToken
            && !delegate.streamHandoffObserved
            && !delegate.streamEnded
            && !delegate.streamTerminalObserved
        ) {
            NSDate *streamDeadline = [NSDate dateWithTimeIntervalSinceNow:MIN(150.0, MAX(0.0, [deadline timeIntervalSinceNow]))];
            while (
                !delegate.streamHandoffObserved
                && !delegate.streamEnded
                && !delegate.streamTerminalObserved
                && [streamDeadline timeIntervalSinceNow] > 0
            ) {
                RunLoopFor(0.1);
            }
        }

        BOOL resumeHandoffWritten = NO;
        if ((delegate.streamResumeToken.length > 0 || delegate.streamTopicId.length > 0) && resumeHandoffFD >= 0) {
            NSString *privateHandoff = PrivateTurnHandoffJSON(delegate);
            resumeHandoffWritten = WriteUTF8ToFD(privateHandoff, resumeHandoffFD);
            close(resumeHandoffFD);
            resumeHandoffFD = -1;
        }
        NSTimeInterval elapsed = [NSDate timeIntervalSinceReferenceDate] - started;
        NSTimeInterval loadElapsed = delegate.navigationFinishedAt > 0 ? delegate.navigationFinishedAt - started : 0;
        PrintResult(@{
            @"ok":@YES,
            @"conversation_id":resolvedConversationId,
            @"response_status":@(delegate.submitResponseObserved ? delegate.submitStatus : (delegate.streamResponseObserved ? delegate.streamStatus : 200)),
            @"submit_request_observed":@(delegate.submitRequestObserved),
            @"submit_response_observed":@(delegate.submitResponseObserved),
            @"submit_response_status":@(delegate.submitStatus),
            @"stream_response_observed":@(delegate.streamResponseObserved),
            @"stream_response_status":@(delegate.streamStatus),
            @"submit_error":delegate.submitError ?: @"",
            @"final_url":webView.URL.absoluteString ?: @"",
            @"elapsed_ms":@((NSInteger)llround(elapsed * 1000.0)),
            @"load_elapsed_ms":@((NSInteger)llround(MAX(0, loadElapsed) * 1000.0)),
            @"attachment_count":@(attachments.count),
            @"profile":profile ?: @"",
            @"write_commit_proven":@(writeCommitProven),
            @"write_commit_proof":resumeCommitFence
                ? @"RESUME_FENCE"
                : (topicHandoffCommitFence
                    ? @"TOPIC_HANDOFF_FENCE"
                    : (streamTerminalCommitFence ? @"STREAM_TERMINAL" : @"CANONICAL")),
            @"canonical_committed":@(canonicalCommitted),
            @"canonical_final_completed":@NO,
            @"canonical_body_base64":@"",
            @"committed_current_node":canonicalCommitted ? (committedCurrentNode ?: @"") : @"",
            @"stream_started":@(delegate.streamStarted),
            @"stream_ended":@(delegate.streamEnded),
            @"stream_terminal_observed":@(delegate.streamTerminalObserved),
            @"stream_resume_present":@(delegate.streamResumeToken.length > 0),
            @"stream_resume_handoff_written":@(resumeHandoffWritten),
            @"stream_handoff_observed":@(delegate.streamHandoffObserved),
            @"stream_topic_id":delegate.streamTopicId ?: @"",
            @"turn_exchange_id":delegate.streamTurnExchangeId ?: @"",
            @"stream_conversation_id":delegate.streamConversationId ?: @"",
            @"bundle_id":@"local.gptty.webkit-authority"
        });
        return 0;
    }
}
