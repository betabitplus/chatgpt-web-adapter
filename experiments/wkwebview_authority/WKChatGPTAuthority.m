#import <Cocoa/Cocoa.h>
#import <WebKit/WebKit.h>

static NSString *ArgValue(NSArray<NSString *> *args, NSString *name, NSString *fallback) {
    NSUInteger idx = [args indexOfObject:name];
    if (idx == NSNotFound || idx + 1 >= args.count) return fallback;
    return args[idx + 1];
}

static BOOL HasArg(NSArray<NSString *> *args, NSString *name) {
    return [args containsObject:name];
}

static NSString *JSONStringLiteral(NSString *value) {
    NSData *data = [NSJSONSerialization dataWithJSONObject:(value ?: @"")
                                                   options:NSJSONWritingFragmentsAllowed
                                                     error:nil];
    NSString *json = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
    return json ?: @"\"\"";
}

@interface WKAuthorityProbe : NSObject <WKNavigationDelegate, WKUIDelegate>
@property(nonatomic, strong) WKWebView *webView;
@property(nonatomic, strong) NSWindow *window;
@property(nonatomic, copy) NSString *attachmentPath;
@end

@implementation WKAuthorityProbe

- (void)webView:(WKWebView *)webView didFinishNavigation:(WKNavigation *)navigation {
    printf("NAV %s\n", webView.URL.absoluteString.UTF8String ?: "");
    fflush(stdout);
}

- (void)webView:(WKWebView *)webView
runOpenPanelWithParameters:(WKOpenPanelParameters *)parameters
initiatedByFrame:(WKFrameInfo *)frame
completionHandler:(void (^)(NSArray<NSURL *> *URLs))completionHandler {
    if (self.attachmentPath.length == 0) {
        completionHandler(@[]);
        return;
    }
    printf("FILE_CHOOSER %s\n", self.attachmentPath.UTF8String);
    fflush(stdout);
    completionHandler(@[[NSURL fileURLWithPath:self.attachmentPath]]);
}

@end

static void Evaluate(WKWebView *webView, NSString *label, NSString *script) {
    [webView evaluateJavaScript:script completionHandler:^(id result, NSError *error) {
        NSString *rendered = result ? [result description] : @"";
        printf("%s %s err=%s\n",
               label.UTF8String,
               rendered.UTF8String ?: "",
               error.localizedDescription.UTF8String ?: "");
        fflush(stdout);
    }];
}

static NSString *ScrollScript(void) {
    return @"(()=>{"
            "for(const e of [document.scrollingElement,...document.querySelectorAll('*')]){"
              "try{if(e&&e.scrollHeight>e.clientHeight)e.scrollTop=e.scrollHeight}catch(_){}}"
            "window.dispatchEvent(new Event('scroll'));"
            "return true;"
            "})()";
}

static NSString *FillScript(NSString *prompt) {
    NSString *text = JSONStringLiteral(prompt ?: @"");
    return [NSString stringWithFormat:
        @"(()=>{"
          "const e=document.querySelector('#prompt-textarea')||document.querySelector('textarea[aria-label=\"Chat with ChatGPT\"]');"
          "if(!e)return JSON.stringify({ok:false,reason:'no_composer',url:location.href});"
          "const text=%@;"
          "e.focus();"
          "if(e.tagName==='TEXTAREA'){"
            "const d=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value');"
            "d.set.call(e,text);"
            "e.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:text}));"
            "e.dispatchEvent(new Event('change',{bubbles:true}));"
          "}else{"
            "e.innerHTML='';"
            "document.execCommand('insertText',false,text);"
            "e.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:text}));"
          "}"
          "return JSON.stringify({ok:true,tag:e.tagName,text:(e.innerText||e.value||'').slice(0,500),url:location.href});"
        "})()", text];
}

static NSString *SendScript(void) {
    return @"(()=>{"
            "const b=document.querySelector('button[data-testid=\"send-button\"]')||"
              "[...document.querySelectorAll('button')].find(x=>/send prompt/i.test(x.getAttribute('aria-label')||''));"
            "if(!b||b.disabled)return JSON.stringify({ok:false,reason:'no_send',url:location.href});"
            "b.click();"
            "return JSON.stringify({ok:true,aria:b.getAttribute('aria-label'),test:b.getAttribute('data-testid'),url:location.href});"
            "})()";
}

static NSString *StopScript(void) {
    return @"(()=>{"
            "const b=document.querySelector('button[data-testid=\"stop-button\"]')||"
              "[...document.querySelectorAll('button')].find(x=>/stop answering/i.test(x.getAttribute('aria-label')||''));"
            "if(!b)return JSON.stringify({ok:false,reason:'no_stop',url:location.href});"
            "b.click();"
            "return JSON.stringify({ok:true,aria:b.getAttribute('aria-label'),test:b.getAttribute('data-testid'),url:location.href});"
            "})()";
}

static NSString *AttachmentClickScript(void) {
    return @"(()=>{"
            "const i=document.querySelector('#upload-photos')||"
              "document.querySelector('input[type=file][accept*=\"image\"]')||"
              "document.querySelector('input[type=file]');"
            "if(!i)return JSON.stringify({ok:false,reason:'no_file_input'});"
            "i.click();"
            "return JSON.stringify({ok:true,id:i.id,accept:i.accept});"
            "})()";
}

static NSString *SnapshotScript(NSString *marker) {
    NSString *needle = JSONStringLiteral(marker ?: @"");
    return [NSString stringWithFormat:
        @"(()=>{"
          "const body=(document.body&&document.body.innerText)||'';"
          "return JSON.stringify({"
            "url:location.href,"
            "title:document.title,"
            "marker:%@,"
            "hasComposer:!!(document.querySelector('#prompt-textarea')||document.querySelector('textarea[aria-label=\"Chat with ChatGPT\"]')),"
            "stop:!!document.querySelector('button[data-testid=\"stop-button\"]'),"
            "tail:body.slice(-2500)"
          "});"
        "})()",
        marker.length ? [NSString stringWithFormat:@"body.includes(%@)", needle] : @"false"];
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSArray<NSString *> *args = [[NSProcessInfo processInfo] arguments];
        NSString *urlString = ArgValue(args, @"--url", @"https://chatgpt.com/");
        NSString *prompt = ArgValue(args, @"--prompt", @"");
        NSString *attachment = ArgValue(args, @"--attach", @"");
        NSString *expectMarker = ArgValue(args, @"--expect-marker", @"");
        NSTimeInterval timeout = [ArgValue(args, @"--timeout", @"60") doubleValue];
        NSTimeInterval stopAfter = [ArgValue(args, @"--stop-after", @"0") doubleValue];
        BOOL visible = HasArg(args, @"--visible") || HasArg(args, @"--login");
        BOOL loginOnly = HasArg(args, @"--login");

        [NSApplication sharedApplication];

        WKWebViewConfiguration *configuration = [WKWebViewConfiguration new];
        configuration.websiteDataStore = [WKWebsiteDataStore defaultDataStore];

        WKWebView *webView = [[WKWebView alloc] initWithFrame:NSMakeRect(0, 0, 1000, 700)
                                               configuration:configuration];
        NSRect rect = visible ? NSMakeRect(0, 0, 1000, 700) : NSMakeRect(-20000, -20000, 1000, 700);
        NSWindowStyleMask style = visible ? (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable)
                                          : NSWindowStyleMaskBorderless;
        NSWindow *window = [[NSWindow alloc] initWithContentRect:rect
                                                       styleMask:style
                                                         backing:NSBackingStoreBuffered
                                                           defer:NO];
        window.title = @"gptty WKWebView Authority";
        window.contentView = webView;
        if (visible) [window center];
        [window orderFront:nil];
        if (visible) {
            [NSApp activateIgnoringOtherApps:YES];
            [window makeKeyAndOrderFront:nil];
        }

        WKAuthorityProbe *probe = [WKAuthorityProbe new];
        probe.webView = webView;
        probe.window = window;
        probe.attachmentPath = attachment;
        webView.navigationDelegate = probe;
        webView.UIDelegate = probe;

        [webView loadRequest:[NSURLRequest requestWithURL:[NSURL URLWithString:urlString]]];

        if (loginOnly) {
            printf("LOGIN_MODE bundle-data-store=default url=%s\n", urlString.UTF8String);
            fflush(stdout);
            [[NSRunLoop mainRunLoop] run];
            return 0;
        }

        if (prompt.length > 0 || attachment.length > 0) {
            dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 12 * NSEC_PER_SEC), dispatch_get_main_queue(), ^{
                Evaluate(webView, @"SCROLL", ScrollScript());
            });
        }

        if (attachment.length > 0) {
            dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 14 * NSEC_PER_SEC), dispatch_get_main_queue(), ^{
                Evaluate(webView, @"ATTACH", AttachmentClickScript());
            });
        }

        if (prompt.length > 0) {
            dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 17 * NSEC_PER_SEC), dispatch_get_main_queue(), ^{
                Evaluate(webView, @"FILL", FillScript(prompt));
            });
            dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 20 * NSEC_PER_SEC), dispatch_get_main_queue(), ^{
                Evaluate(webView, @"SEND", SendScript());
            });
        }

        if (stopAfter > 0) {
            dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)((20.0 + stopAfter) * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
                Evaluate(webView, @"STOP", StopScript());
            });
        }

        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(timeout * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
            [webView evaluateJavaScript:SnapshotScript(expectMarker) completionHandler:^(id result, NSError *error) {
                if (error) {
                    printf("SNAPSHOT_ERROR %s\n", error.localizedDescription.UTF8String ?: "");
                } else {
                    printf("SNAPSHOT %s\n", [[result description] UTF8String] ?: "");
                }
                fflush(stdout);
                exit(0);
            }];
        });

        [[NSRunLoop mainRunLoop] run];
    }
    return 0;
}
