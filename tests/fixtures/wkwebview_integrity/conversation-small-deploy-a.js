// Sanitized structural fixture from an observed conversation-small deployment.
// Identifiers and export aliases are intentionally synthetic; no session or token values are retained.
import{x as X}from"./noise-a.js";
import{a as A,b as B,c as C,d as D,e as E}from"./shared-runtime-a.js";
const noiseA = 1;
function qA(t){
  const e=t.chatReq,r=t.turnstileToken,l=t.proofToken;
  return {chatReq:e,turnstileToken:r,proofToken:l};
}
var varInitA=e((()=>{const ready=true;return ready}));
async function*streamA(url,{shouldRetry:t=()=>!0,retryConfig:{MIN_RETRY_INTERVAL:n,MAX_RETRY_INTERVAL:r,RETRY_FACTOR:i,MAX_RETRY_COUNT:a},onRetry:o,...s}){
  yield {url,s};
}
var delayA,transportInitA=e((()=>{delayA=1}));
export{qA as hA,varInitA as iA,streamA as sA,transportInitA as tA};
