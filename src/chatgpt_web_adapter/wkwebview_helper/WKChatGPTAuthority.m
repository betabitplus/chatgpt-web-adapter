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
@property(nonatomic, assign) BOOL submitResponseObserved;
@property(nonatomic, assign) NSInteger submitStatus;
@property(nonatomic, strong) NSString *submitError;
@property(nonatomic, assign) BOOL streamHandoffObserved;
@property(nonatomic, assign) BOOL streamStarted;
@property(nonatomic, assign) BOOL streamResponseObserved;
@property(nonatomic, assign) NSInteger streamStatus;
@property(nonatomic, assign) BOOL streamEnded;
@property(nonatomic, assign) BOOL streamTerminalObserved;
@property(nonatomic, strong) NSString *streamResumeToken;
@property(nonatomic, strong) NSString *streamTopicId;
@property(nonatomic, strong) NSString *streamTurnExchangeId;
@property(nonatomic, strong) NSString *streamConversationId;
@property(nonatomic, strong) NSString *streamStopConduitToken;
@property(nonatomic, strong) NSString *streamTurnTraceId;
@property(nonatomic, strong) NSString *streamClientMessageId;
@end

@implementation WKAuthorityDelegate

- (void)webView:(WKWebView *)webView didFinishNavigation:(WKNavigation *)navigation {
    self.navigationFinished = YES;
    self.navigationFinishedAt = [NSDate timeIntervalSinceReferenceDate];
    PrintEvent(@{@"type":@"navigation_finished"});
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

- (void)userContentController:(WKUserContentController *)userContentController
      didReceiveScriptMessage:(WKScriptMessage *)message {
    if ([message.name isEqualToString:@"cwaCanonical"]) {
        if ([message.body isKindOfClass:[NSDictionary class]]) {
            self.canonicalResult = (NSDictionary *)message.body;
        } else {
            self.canonicalResult = @{@"ok":@NO,@"error":@"CANONICAL_READ_MESSAGE_INVALID"};
        }
        self.canonicalDone = YES;
        return;
    }
    if ([message.name isEqualToString:@"cwaStream"] && [message.body isKindOfClass:[NSDictionary class]]) {
        NSDictionary *body = (NSDictionary *)message.body;
        NSString *phase = [body[@"phase"] isKindOfClass:[NSString class]] ? body[@"phase"] : @"";
        if ([phase isEqualToString:@"started"]) {
            self.streamStarted = YES;
            NSNumber *status = [body[@"status"] isKindOfClass:[NSNumber class]] ? body[@"status"] : nil;
            if (status != nil) {
                self.streamResponseObserved = YES;
                self.streamStatus = [status integerValue];
            }
        }
        else if ([phase isEqualToString:@"ended"] || [phase isEqualToString:@"done"]) self.streamEnded = YES;
        else if ([phase isEqualToString:@"terminal"]) self.streamTerminalObserved = YES;
        else if ([phase isEqualToString:@"resume"]) {
            self.streamResumeToken = [body[@"token"] isKindOfClass:[NSString class]] ? body[@"token"] : nil;
            NSString *resumeConversationId = [body[@"conversation_id"] isKindOfClass:[NSString class]] ? body[@"conversation_id"] : nil;
            if (resumeConversationId.length > 0) self.streamConversationId = resumeConversationId;
            PrintEvent(@{@"type":@"stream_resume_token_observed",@"token_present":@(self.streamResumeToken.length > 0),@"conversation_id_present":@(self.streamConversationId.length > 0)});
        } else if ([phase isEqualToString:@"stop_context"]) {
            self.streamStopConduitToken = [body[@"conduit_token"] isKindOfClass:[NSString class]] ? body[@"conduit_token"] : nil;
            self.streamTurnTraceId = [body[@"turn_trace_id"] isKindOfClass:[NSString class]] ? body[@"turn_trace_id"] : nil;
        } else if ([phase isEqualToString:@"client_message"]) {
            self.streamClientMessageId = [body[@"message_id"] isKindOfClass:[NSString class]] ? body[@"message_id"] : nil;
        } else if ([phase isEqualToString:@"text"]) {
            NSString *eventType = [body[@"type"] isKindOfClass:[NSString class]] ? body[@"type"] : @"";
            if ([eventType isEqualToString:@"assistant_text_snapshot"]
                || [eventType isEqualToString:@"assistant_text_delta"]
                || [eventType isEqualToString:@"assistant_text_revision"]) {
                NSMutableDictionary *event = [body mutableCopy];
                [event removeObjectForKey:@"phase"];
                PrintEvent(event);
            }
        } else if ([phase isEqualToString:@"handoff"]) {
            self.streamHandoffObserved = YES;
            self.streamTopicId = [body[@"topic_id"] isKindOfClass:[NSString class]] ? body[@"topic_id"] : nil;
            self.streamTurnExchangeId = [body[@"turn_exchange_id"] isKindOfClass:[NSString class]] ? body[@"turn_exchange_id"] : nil;
            self.streamConversationId = [body[@"conversation_id"] isKindOfClass:[NSString class]] ? body[@"conversation_id"] : nil;
            PrintEvent(@{
                @"type":@"stream_handoff_probe",
                @"topic_present":@(self.streamTopicId.length > 0),
                @"turn_exchange_id_present":@(self.streamTurnExchangeId.length > 0),
                @"conversation_id_present":@(self.streamConversationId.length > 0)
            });
        }
        return;
    }
    if ([message.name isEqualToString:@"cwaSubmit"] && [message.body isKindOfClass:[NSDictionary class]]) {
        NSDictionary *body = (NSDictionary *)message.body;
        NSString *phase = [body[@"phase"] isKindOfClass:[NSString class]] ? body[@"phase"] : @"";
        if ([phase isEqualToString:@"request"]) {
            self.submitRequestObserved = YES;
            self.submitTemporaryModeObserved = [body[@"temporary_mode"] boolValue];
            PrintEvent(@{@"type":@"submit_request_observed",@"temporary_mode":@(self.submitTemporaryModeObserved)});
        } else if ([phase isEqualToString:@"response"]) {
            self.submitRequestObserved = YES;
            self.submitResponseObserved = YES;
            NSNumber *status = [body[@"status"] isKindOfClass:[NSNumber class]] ? body[@"status"] : @0;
            self.submitStatus = status.integerValue;
            PrintEvent(@{@"type":@"submit_response_observed",@"status":status});
        } else if ([phase isEqualToString:@"error"]) {
            self.submitRequestObserved = YES;
            self.submitError = [body[@"error"] isKindOfClass:[NSString class]] ? body[@"error"] : @"SUBMIT_FETCH_FAILED";
        }
    }
}

@end

static NSString *PrivateTurnHandoffJSON(WKAuthorityDelegate *delegate) {
    NSDictionary *payload = @{
        @"v": @1,
        @"r": delegate.streamResumeToken ?: @"",
        @"c": delegate.streamStopConduitToken ?: @"",
        @"t": delegate.streamTurnTraceId ?: @""
    };
    NSData *data = [NSJSONSerialization dataWithJSONObject:payload options:0 error:nil];
    if (data.length == 0) return nil;
    return [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
}

static NSString *SubmitObservationScript(void) {
    return @"(()=>{"
            "if(window.__cwaSubmitObserverInstalled)return;window.__cwaSubmitObserverInstalled=true;"
            "const originalFetch=window.fetch;"
            "window.fetch=async function(input,init){"
              "const url=typeof input==='string'?input:((input&&input.url)||'');"
              "const method=((init&&init.method)||(input&&input.method)||'GET').toUpperCase();"
              "const watched=method==='POST'&&/\\/backend-api\\/f\\/conversation(?:$|[?#])/.test(url);"
              "if(watched){let temporary=false;try{const raw=init&&init.body;if(typeof raw==='string'){const payload=JSON.parse(raw);temporary=payload&&payload.history_and_training_disabled===true;}}catch(_){}try{window.webkit.messageHandlers.cwaSubmit.postMessage({phase:'request',temporary_mode:temporary});}catch(_){}}"
              "try{"
                "const response=await originalFetch.apply(this,arguments);"
                "if(watched){try{window.webkit.messageHandlers.cwaSubmit.postMessage({phase:'response',status:response.status});}catch(_){}}"
                "return response;"
              "}catch(error){"
                "if(watched){try{window.webkit.messageHandlers.cwaSubmit.postMessage({phase:'error',error:String(error)});}catch(_){}}"
                "throw error;"
              "}"
            "};"
            "})()";
}

static NSString *PassiveStreamObservationScript(void) {
    return @"(()=>{"
            "if(window.__cwaWKStreamObservationInstalled)return;window.__cwaWKStreamObservationInstalled=true;"
            "const originalFetch=window.fetch;if(typeof originalFetch!=='function')return;"
            "const post=(body)=>{try{window.webkit.messageHandlers.cwaStream.postMessage(body)}catch(_){}};"
            "const str=(v)=>typeof v==='string'&&v.trim()?v.trim():null;"
            "let sequence=0,currentMessageId=null,currentRecipient='all',currentText='',currentIsFinalText=false;"
            "const contentText=(content)=>{if(!content||typeof content!=='object')return '';if(typeof content.text==='string')return content.text;if(typeof content.content==='string')return content.content;const parts=Array.isArray(content.parts)?content.parts:[];let out='';for(const part of parts.slice(0,64)){if(typeof part==='string')out+=part;else if(part&&typeof part.text==='string')out+=part.text;}return out;};"
            "const emitText=(type,id,value)=>{sequence+=1;const event={phase:'text',type,sequence,message_id:id||null};if(type==='assistant_text_delta')event.delta=value;else event.text=value;post(event);};"
            "const applyText=(text)=>{if(typeof text!=='string'||!currentMessageId||currentRecipient!=='all'||text===currentText)return;if(text.startsWith(currentText)){const delta=text.slice(currentText.length);currentText=text;if(delta)emitText('assistant_text_delta',currentMessageId,delta);}else{currentText=text;emitText('assistant_text_revision',currentMessageId,text);}};"
            "const completedStatus=(value)=>['completed','complete','finished','done','success','succeeded','finished_successfully'].includes(String(value||'').toLowerCase());"
            "const messageTerminal=(message)=>{if(!message||typeof message!=='object')return false;const metadata=message.metadata&&typeof message.metadata==='object'?message.metadata:{};const finishDetails=metadata.finish_details&&typeof metadata.finish_details==='object'?metadata.finish_details:null;return message.end_turn===true||completedStatus(message.status)||completedStatus(message.async_status)||completedStatus(metadata.status)||completedStatus(metadata.async_status)||(finishDetails&&!!str(finishDetails.type))||!!str(metadata.finish_reason)||!!str(message.finish_reason);};"
            "const emitTerminal=()=>post({phase:'terminal',message_id:currentMessageId||null});"
            "const inspectTerminalPatch=(path,value)=>{if(!currentIsFinalText)return;const p=String(path||'');if((p==='/message/end_turn'&&value===true)||(p==='/message/status'&&completedStatus(value))||(p==='/message/async_status'&&completedStatus(value))||(p==='/message/metadata/status'&&completedStatus(value))||(p==='/message/metadata/async_status'&&completedStatus(value))||(p==='/message/metadata/finish_reason'&&!!str(value))||(p==='/message/finish_reason'&&!!str(value))||(p==='/message/metadata/finish_details'&&value&&typeof value==='object'&&!!str(value.type))){emitTerminal();}};"
            "const selectMessage=(message)=>{if(!message||typeof message!=='object')return;const id=str(message.id);const previousMessageId=currentMessageId;const role=str(message.author&&message.author.role)||'';currentRecipient=str(message.recipient)||'all';const contentType=str(message.content&&message.content.content_type)||'';if(id)currentMessageId=id;currentIsFinalText=role==='assistant'&&currentRecipient==='all'&&contentType==='text'&&!(message.metadata&&message.metadata.is_thinking_preamble_message===true);if(!currentIsFinalText)return;const text=contentText(message.content);if(id&&id!==previousMessageId){currentText='';if(text){currentText=text;emitText('assistant_text_snapshot',currentMessageId,text);}}else if(currentText===''&&text){currentText=text;emitText('assistant_text_snapshot',currentMessageId,text);}else applyText(text);if(messageTerminal(message))emitTerminal();};"
            "const inspectIdentity=(value,depth=0)=>{if(value==null||depth>7)return;if(Array.isArray(value)){for(const item of value.slice(0,128))inspectIdentity(item,depth+1);return;}if(typeof value!=='object')return;if(value.type==='resume_conversation_token'&&str(value.token)){post({phase:'resume',token:str(value.token),conversation_id:str(value.conversation_id)});}if(value.type==='stream_handoff'){let topic=null;const options=Array.isArray(value.options)?value.options:[];for(const option of options.slice(0,16)){if(option&&option.type==='subscribe_ws_topic'&&str(option.topic_id)){topic=str(option.topic_id);break;}}post({phase:'handoff',topic_id:topic,conversation_id:str(value.conversation_id),turn_exchange_id:str(value.turn_exchange_id)});}for(const key of ['message','messages','data','result','payload','turn','v','value']){if(Object.prototype.hasOwnProperty.call(value,key))inspectIdentity(value[key],depth+1);}};"
            "const processPayload=(payload)=>{inspectIdentity(payload);if(!payload||typeof payload!=='object')return;const value=payload.v,path=payload.p;if(value&&typeof value==='object'&&!Array.isArray(value)&&value.message)selectMessage(value.message);if(typeof value==='string'&&currentRecipient==='all'&&(path==null||path==='/message/content/parts/0')){currentText+=value;emitText('assistant_text_delta',currentMessageId,value);}inspectTerminalPatch(path,value);if(Array.isArray(value)){for(const item of value.slice(0,128)){if(!item||typeof item!=='object')continue;if(item.v&&typeof item.v==='object'&&!Array.isArray(item.v)&&item.v.message)selectMessage(item.v.message);if(item.p==='/message/content/parts/0'&&typeof item.v==='string'&&currentRecipient==='all'){currentText+=item.v;emitText('assistant_text_delta',currentMessageId,item.v);}else if(item.p==='/message/content'&&item.v&&typeof item.v==='object'&&currentRecipient==='all'){applyText(contentText(item.v));}inspectTerminalPatch(item.p,item.v);}}};"
            "const isWrite=(url,method)=>{if(String(method||'GET').toUpperCase()!=='POST')return false;try{const u=new URL(url,location.href);const p=u.pathname.replace(/\\/+$/,'');return u.origin===location.origin&&(p.endsWith('/backend-api/conversation')||p.endsWith('/backend-api/f/conversation')||p.endsWith('/backend-api/f/conversation/resume'));}catch(_){return false;}};"
            "const processBlock=(block)=>{const data=String(block||'').split(/\\r?\\n/).filter(line=>line.startsWith('data:')).map(line=>line.slice(5).trimStart()).join('\\n').trim();if(!data)return;if(data==='[DONE]'){post({phase:'done'});return;}try{processPayload(JSON.parse(data));}catch(_){}};"
            "const observe=async(response)=>{if(!response||!response.body)return;post({phase:'started',status:Number(response.status)||0,ok:response.ok===true});const reader=response.body.getReader();const decoder=new TextDecoder();let buffer='';try{while(true){const chunk=await reader.read();if(chunk.done){buffer+=decoder.decode();break;}buffer+=decoder.decode(chunk.value,{stream:true});if(buffer.length>1000000)buffer=buffer.slice(-1000000);while(true){const m=/\\r?\\n\\r?\\n/.exec(buffer);if(!m)break;const block=buffer.slice(0,m.index);buffer=buffer.slice(m.index+m[0].length);processBlock(block);}}const tail=buffer.trim();if(tail)processBlock(tail);}catch(_){}finally{try{reader.releaseLock()}catch(_){}post({phase:'ended'});}};"
            "window.fetch=new Proxy(originalFetch,{apply(target,thisArg,args){return Reflect.apply(target,thisArg,args).then(response=>{let url='';let method='GET';try{const input=args[0],init=args[1];if(input instanceof Request){url=input.url;method=(init&&init.method)||input.method;}else{url=String(input||'');method=(init&&init.method)||'GET';}}catch(_){}if(isWrite(url,method)){try{void observe(response.clone())}catch(_){}}return response;});}});"
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

static NSString *ReadinessScript(void) {
    return @"(()=>{"
            "const editor=document.querySelector('div.ProseMirror[contenteditable=\"true\"]')||document.querySelector('div[role=\"textbox\"][contenteditable=\"true\"]');const fallback=document.querySelector('#prompt-textarea')||document.querySelector('textarea[aria-label=\"Chat with ChatGPT\"]');const composer=editor||fallback;const composerFallback=!!(composer&&composer===fallback&&/fallbackTextarea/i.test(String(composer.className||'')));const composerReady=!!composer&&!composerFallback;"
            "const composerTag=composer?composer.tagName:null;const composerContentEditable=composer?composer.getAttribute('contenteditable'):null;const composerClass=composer?(composer.className||'').toString().slice(0,200):null;"
            "const send=document.querySelector('button[data-testid=\"send-button\"]')||[...document.querySelectorAll('button')].find(b=>/send prompt/i.test(b.getAttribute('aria-label')||''));"
            "const stop=document.querySelector('button[data-testid=\"stop-button\"]')||[...document.querySelectorAll('button')].find(b=>/stop answering/i.test(b.getAttribute('aria-label')||''));"
            "const body=(document.body&&document.body.innerText)||'';"
            "const messageNodes=[...document.querySelectorAll('[data-message-id]')];"
            "const latestMessageId=messageNodes.length?(messageNodes[messageNodes.length-1].getAttribute('data-message-id')||null):null;"
            "let selectedMode=null;const modeCandidates=[];"
            "if(composer){const cr=composer.getBoundingClientRect();for(const e of document.querySelectorAll('button,[role=button],span,div')){if(e.children.length)continue;const t=(e.textContent||'').trim();if(!/^(Instant|Medium|High)$/i.test(t))continue;const r=e.getBoundingClientRect();if(!r.width||!r.height)continue;const d=Math.hypot((r.left+r.width/2)-(cr.left+cr.width/2),(r.top+r.height/2)-(cr.top+cr.height/2));modeCandidates.push({text:t,distance:Math.round(d)});}modeCandidates.sort((a,b)=>a.distance-b.distance);if(modeCandidates.length&&modeCandidates[0].distance<700)selectedMode=modeCandidates[0].text;}"
            "const bodyTail=body.slice(-1200);if(!selectedMode){const lines=bodyTail.split(/\\n+/).map(x=>x.trim()).filter(x=>/^(Instant|Medium|High)$/i.test(x));if(lines.length)selectedMode=lines[lines.length-1];}"
            "return JSON.stringify({url:location.href,title:document.title,composer:!!composer,composerReady,composerFallback,composerTag,composerContentEditable,composerClass,send:!!send&&!send.disabled&&send.getAttribute('aria-disabled')!=='true',stop:!!stop,latestMessageId,selectedMode,modeCandidates:modeCandidates.slice(0,8),login:/log in|sign up/i.test(body.slice(0,4000)),bodyTail});"
            "})()";
}

static NSString *FillScript(NSString *prompt) {
    NSString *literal = JSONStringLiteral(prompt ?: @"");
    return [NSString stringWithFormat:
            @"(()=>{"
              "const e=document.querySelector('div.ProseMirror[contenteditable=\"true\"]')||document.querySelector('div[role=\"textbox\"][contenteditable=\"true\"]')||document.querySelector('#prompt-textarea')||document.querySelector('textarea[aria-label=\"Chat with ChatGPT\"]');"
              "if(!e)return JSON.stringify({ok:false,reason:'no_composer'});"
              "const text=%@;e.focus();"
              "if(e.tagName==='TEXTAREA'){"
                "const d=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value');d.set.call(e,text);"
                "e.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:text}));"
                "e.dispatchEvent(new Event('change',{bubbles:true}));"
              "}else{"
                "document.execCommand('selectAll',false,null);"
                "document.execCommand('insertText',false,text);"
              "}"
              "return JSON.stringify({ok:true,text:(e.innerText||e.value||'').slice(0,500)});"
            "})()", literal];
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

static NSString *SendScript(void) {
    return @"(()=>{"
            "const b=document.querySelector('button[data-testid=\"send-button\"]')||[...document.querySelectorAll('button')].find(x=>/send prompt/i.test(x.getAttribute('aria-label')||''));"
            "if(!b||b.disabled)return JSON.stringify({ok:false,reason:'no_send'});b.click();"
            "return JSON.stringify({ok:true,aria:b.getAttribute('aria-label'),test:b.getAttribute('data-testid')});"
            "})()";
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
              "const url=%@;"
              "fetch('/api/auth/session',{credentials:'include',cache:'no-store'})"
                ".then(async s=>{const session=await s.json();const token=session&&session.accessToken;if(!token)throw new Error('AUTH_SESSION_ACCESS_TOKEN_MISSING');return fetch(url,{credentials:'include',cache:'no-store',headers:{Authorization:'Bearer '+token}});})"
                ".then(async r=>{const body=await r.text();window.webkit.messageHandlers.cwaCanonical.postMessage({ok:r.ok,status:r.status,contentType:r.headers.get('content-type')||'',body});})"
                ".catch(e=>window.webkit.messageHandlers.cwaCanonical.postMessage({ok:false,status:0,contentType:'',error:String(e)}));"
              "return true;"
            "})()", literal];
}

static NSString *StopConversationFetchScript(
    NSString *conversationId,
    NSString *conduitToken,
    NSString *turnTraceId
) {
    NSString *idLiteral = JSONStringLiteral(conversationId ?: @"");
    NSString *conduitLiteral = JSONStringLiteral(conduitToken ?: @"");
    NSString *traceLiteral = JSONStringLiteral(turnTraceId ?: @"");
    return [NSString stringWithFormat:
            @"(()=>{"
              "const id=%@,conduit=%@,trace=%@;"
              "fetch('/api/auth/session',{credentials:'include',cache:'no-store'})"
                ".then(async s=>{const session=await s.json();const token=session&&session.accessToken;if(!token)throw new Error('AUTH_SESSION_ACCESS_TOKEN_MISSING');const headers={Accept:'application/json','Content-Type':'application/json',Authorization:'Bearer '+token};if(conduit)headers['x-conduit-token']=conduit;if(trace)headers['x-oai-turn-trace-id']=trace;return fetch('/backend-api/stop_conversation',{method:'POST',credentials:'include',cache:'no-store',headers,body:JSON.stringify({conversation_id:id,exclude_async_types:[]})});})"
                ".then(async r=>{const body=await r.text();window.webkit.messageHandlers.cwaCanonical.postMessage({ok:r.ok,status:r.status,body});})"
                ".catch(e=>window.webkit.messageHandlers.cwaCanonical.postMessage({ok:false,status:0,error:String(e)}));"
              "return true;"
            "})()", idLiteral, conduitLiteral, traceLiteral];
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
              "const id=%@,url='/backend-api/conversation/'+encodeURIComponent(id);"
              "const completed=v=>['completed','complete','finished','done','success','succeeded','finished_successfully'].includes(String(v||'').toLowerCase());"
              "fetch('/api/auth/session',{credentials:'include',cache:'no-store'})"
                ".then(async s=>{const session=await s.json();const token=session&&session.accessToken;if(!token)throw new Error('AUTH_SESSION_ACCESS_TOKEN_MISSING');return fetch(url,{credentials:'include',cache:'no-store',headers:{Authorization:'Bearer '+token}});})"
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
              "const id=%@,expectedPrompt=%@,baseline=%@;"
              "const url='/backend-api/conversation/'+encodeURIComponent(id);"
              "fetch('/api/auth/session',{credentials:'include',cache:'no-store'})"
                ".then(async s=>{const session=await s.json();const token=session&&session.accessToken;if(!token)throw new Error('AUTH_SESSION_ACCESS_TOKEN_MISSING');return fetch(url,{credentials:'include',cache:'no-store',headers:{Authorization:'Bearer '+token}});})"
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
    return @"<!doctype html><meta charset='utf-8'><title>cwa security shell</title>"
            "<script>window.__cwaSentinelLoaded=false</script>"
            "<script src='/backend-api/sentinel/sdk.js' onload='window.__cwaSentinelLoaded=true'></script>"
            "<body></body>";
}

static NSString *MinimalSecurityWriteScript(
    NSString *prompt,
    NSString *profile,
    NSString *conversationId,
    NSString *parentMessageId,
    NSString *selectedModelSlug,
    NSString *selectedThinkingEffort,
    NSString *attachmentsBase64,
    BOOL temporary
) {
    NSURL *resourceURL = [[NSBundle mainBundle] URLForResource:@"minimal_security_shell" withExtension:@"js"];
    if (resourceURL == nil) return nil;
    NSError *error = nil;
    NSString *source = [NSString stringWithContentsOfURL:resourceURL encoding:NSUTF8StringEncoding error:&error];
    if (source.length == 0 || error != nil) return nil;
    NSString *promptLiteral = JSONStringLiteral(prompt ?: @"");
    NSString *profileLiteral = JSONStringLiteral(profile ?: @"");
    NSString *conversationLiteral = JSONStringLiteral(conversationId ?: @"");
    NSString *parentLiteral = JSONStringLiteral(parentMessageId ?: @"");
    NSString *modelLiteral = JSONStringLiteral(selectedModelSlug ?: @"");
    NSString *effortLiteral = JSONStringLiteral(selectedThinkingEffort ?: @"");
    NSString *attachmentsLiteral = JSONStringLiteral(attachmentsBase64 ?: @"");
    NSString *temporaryLiteral = temporary ? @"true" : @"false";
    return [NSString stringWithFormat:
        @"window.__CWA_MINIMAL_PROMPT__=%@;window.__CWA_MINIMAL_PROFILE__=%@;window.__CWA_MINIMAL_TEMPORARY__=%@;window.__CWA_MINIMAL_CONVERSATION_ID__=%@;window.__CWA_MINIMAL_PARENT_MESSAGE_ID__=%@;window.__CWA_MINIMAL_SELECTED_MODEL_SLUG__=%@;window.__CWA_MINIMAL_SELECTED_THINKING_EFFORT__=%@;window.__CWA_MINIMAL_ATTACHMENTS_BASE64__=%@;\n%@",
        promptLiteral,
        profileLiteral,
        temporaryLiteral,
        conversationLiteral,
        parentLiteral,
        modelLiteral,
        effortLiteral,
        attachmentsLiteral,
        source
    ];
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
        NSString *stopContextConduit = RequestString(request, @"stop_context_conduit", @"");
        NSString *stopContextTrace = RequestString(request, @"stop_context_trace", @"");
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
        if (readOnly) urlString = @"https://chatgpt.com/robots.txt";
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
                if (responseOK && delegate.streamClientMessageId.length > 0 && identityRecoveryDeadline == nil) {
                    identityRecoveryDeadline = [NSDate dateWithTimeIntervalSinceNow:8.0];
                }
                BOOL terminalCompletionFence = responseOK
                    && minimalConversationId.length > 0
                    && delegate.streamTerminalObserved
                    && delegate.streamConversationId.length > 0;
                if (terminalCompletionFence) break;
                if (responseOK && delegate.streamResumeToken.length > 0 && delegate.streamConversationId.length > 0) break;
                if (identityRecoveryDeadline != nil && [identityRecoveryDeadline timeIntervalSinceNow] <= 0) break;
            }
            BOOL responseOK = (delegate.streamResponseObserved && delegate.streamStatus >= 200 && delegate.streamStatus < 300)
                || (delegate.submitResponseObserved && delegate.submitStatus >= 200 && delegate.submitStatus < 300);
            BOOL terminalCompletionFence = responseOK
                && minimalConversationId.length > 0
                && delegate.streamTerminalObserved
                && delegate.streamConversationId.length > 0;
            BOOL resumeFenceObserved = responseOK
                && delegate.streamResumeToken.length > 0
                && delegate.streamConversationId.length > 0;
            BOOL identityRecoveryRequired = !terminalCompletionFence
                && !resumeFenceObserved
                && responseOK
                && delegate.streamClientMessageId.length > 0;
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
                    @"client_message_id":delegate.streamClientMessageId,
                    @"conversation_id":delegate.streamConversationId ?: @"",
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
            if (!terminalCompletionFence && !resumeFenceObserved) {
                PrintResult(@{
                    @"ok":@NO,
                    @"error":@"WKWEBVIEW_MINIMAL_SECURITY_RESUME_FENCE_MISSING",
                    @"submit_response_status":@(delegate.submitStatus),
                    @"stream_response_status":@(delegate.streamStatus),
                    @"resume_token_present":@(delegate.streamResumeToken.length > 0),
                    @"conversation_id_present":@(delegate.streamConversationId.length > 0)
                });
                return 34;
            }
            NSString *writeCommitProof = resumeFenceObserved ? @"RESUME_FENCE" : @"PHASE_A_TERMINAL";
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
                delegate.canonicalDone = NO;
                delegate.canonicalResult = nil;
                EvaluateSync(
                    webView,
                    StopConversationFetchScript(
                        stopConversation,
                        stopContextConduit,
                        stopContextTrace
                    ),
                    2.0,
                    nil
                );
                NSDate *stopRequestDeadline = [NSDate dateWithTimeIntervalSinceNow:MIN(8.0, MAX(0.1, [deadline timeIntervalSinceNow]))];
                while (!delegate.canonicalDone && [stopRequestDeadline timeIntervalSinceNow] > 0) {
                    RunLoopFor(0.05);
                }
                NSDictionary *stopResult = delegate.canonicalResult;
                BOOL stopRequestOK = [stopResult isKindOfClass:[NSDictionary class]] && [stopResult[@"ok"] boolValue];
                NSNumber *stopStatus = [stopResult[@"status"] isKindOfClass:[NSNumber class]] ? stopResult[@"status"] : @0;
                if (!stopRequestOK) {
                    PrintResult(@{
                        @"ok":@NO,
                        @"error":@"WKWEBVIEW_STOP_HTTP_FAILED",
                        @"status":stopStatus,
                        @"conversation_id":stopConversation
                    });
                    return 29;
                }
                PrintResult(@{
                    @"ok":@YES,
                    @"stop_requested":@YES,
                    @"status":stopStatus,
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
                NSTimeInterval nextCanonicalCheckAt = [NSDate timeIntervalSinceReferenceDate];
                while (!canonicalCompleted && [deadline timeIntervalSinceNow] > 0) {
                    RunLoopFor(0.05);
                    NSTimeInterval now = [NSDate timeIntervalSinceReferenceDate];
                    if (now < nextCanonicalCheckAt) continue;
                    nextCanonicalCheckAt = now + 2.0;
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
                    RunLoopFor(MIN(observerPollInterval, remaining));
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
            NSDictionary *sent = ParseJSONResult(EvaluateSync(webView, SendScript(), 1.0, nil));
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
            && !delegate.streamEnded
        ) {
            NSDate *resumeTokenDeadline = [NSDate dateWithTimeIntervalSinceNow:MIN(20.0, MAX(0.0, [deadline timeIntervalSinceNow]))];
            while (
                delegate.streamResumeToken.length == 0
                && !delegate.streamEnded
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

        BOOL canonicalCommitted = NO;
        NSString *committedCurrentNode = @"";
        BOOL heavyFinalCanonicalCompleted = NO;
        NSString *heavyFinalCanonicalBody = @"";
        NSString *heavyFinalCurrentNode = @"";
        BOOL streamingResumeMode = observeStream && streamObserveUntilResumeToken;

        if (streamingResumeMode && !resumeCommitFence) {
            NSTimeInterval nextCanonicalCheckAt = [NSDate timeIntervalSinceReferenceDate];
            while (!resumeCommitFence && !heavyFinalCanonicalCompleted && [deadline timeIntervalSinceNow] > 0) {
                RunLoopFor(0.05);

                resumeConversationMatches = delegate.streamConversationId.length > 0
                    && [delegate.streamConversationId isEqualToString:resolvedConversationId];
                resumeCommitFence = accepted != nil
                    && submitSucceeded
                    && delegate.streamResumeToken.length > 0
                    && resumeConversationMatches;
                if (resumeCommitFence) break;

                NSTimeInterval now = [NSDate timeIntervalSinceReferenceDate];
                if (now < nextCanonicalCheckAt) continue;
                nextCanonicalCheckAt = now + 2.0;
                delegate.canonicalDone = NO;
                delegate.canonicalResult = nil;
                EvaluateSync(webView, CanonicalCompletionCheckScript(resolvedConversationId), 1.5, nil);
                NSDate *canonicalDeadline = [NSDate dateWithTimeIntervalSinceNow:MIN(2.0, MAX(0.05, [deadline timeIntervalSinceNow]))];
                while (!delegate.canonicalDone && [canonicalDeadline timeIntervalSinceNow] > 0) RunLoopFor(0.025);
                NSDictionary *finalProof = delegate.canonicalResult;
                heavyFinalCanonicalCompleted = [finalProof isKindOfClass:[NSDictionary class]]
                    && [finalProof[@"completed"] boolValue];
                if (heavyFinalCanonicalCompleted) {
                    heavyFinalCanonicalBody = [finalProof[@"body"] isKindOfClass:[NSString class]] ? finalProof[@"body"] : @"";
                    heavyFinalCurrentNode = [finalProof[@"currentNode"] isKindOfClass:[NSString class]] ? finalProof[@"currentNode"] : @"";
                }
            }
        } else if (!resumeCommitFence) {
            NSDate *commitDeadline = [NSDate dateWithTimeIntervalSinceNow:MIN(30.0, MAX(1.0, [deadline timeIntervalSinceNow]))];
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
                RunLoopFor(0.2);
            }
        }

        BOOL writeCommitProven = canonicalCommitted || resumeCommitFence || heavyFinalCanonicalCompleted;
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
                @"heavy_final_canonical_completed":@(heavyFinalCanonicalCompleted)
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

        NSData *heavyFinalBodyData = [heavyFinalCanonicalBody dataUsingEncoding:NSUTF8StringEncoding] ?: [NSData data];
        NSString *heavyFinalBodyBase64 = [heavyFinalBodyData base64EncodedStringWithOptions:0] ?: @"";
        BOOL resumeHandoffWritten = NO;
        if (delegate.streamResumeToken.length > 0 && resumeHandoffFD >= 0) {
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
            @"write_commit_proof":resumeCommitFence ? @"RESUME_FENCE" : (heavyFinalCanonicalCompleted ? @"FINAL_CANONICAL" : @"CANONICAL"),
            @"canonical_committed":@(canonicalCommitted),
            @"canonical_final_completed":@(heavyFinalCanonicalCompleted),
            @"canonical_body_base64":heavyFinalBodyBase64,
            @"committed_current_node":canonicalCommitted ? (committedCurrentNode ?: @"") : (heavyFinalCurrentNode ?: @""),
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
