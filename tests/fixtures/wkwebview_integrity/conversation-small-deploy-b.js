// Sanitized structural fixture from a later observed conversation-small deployment.
// Internal names, aliases, and shared-runtime path differ from deploy A.
import{y as Y}from"./noise-b.js";
import{aa as Alpha,bb as Beta,cc as Gamma,dd as Delta,ee as Epsilon,ff as Zeta}from"./shared-runtime-b.js";
const noiseB = "layout-b";
function rB(input){
  const e=input.chatReq,q=input.turnstileToken,p=input.proofToken;
  return {chatReq:e,turnstileToken:q,proofToken:p,extra:true};
}
var initB=e((()=>{const state={ready:true};return state}));
async function*renamedStreamB(url,{shouldRetry:t=()=>!0,retryConfig:{MIN_RETRY_INTERVAL:n,MAX_RETRY_INTERVAL:r,RETRY_FACTOR:i,MAX_RETRY_COUNT:a},onRetry:o,...rest}){
  yield {url,rest};
}
var waitB,initTransportB=e((()=>{waitB=2}));
export{rB as renamedHelperB,initB as renamedInitializerB,renamedStreamB as renamedTransportB,initTransportB as renamedTransportInitializerB};
