// Sanitized structural fixture from the later observed conversation-small deployment.
// The helper/internal initializer names and public aliases differ from deploy A.
const noiseB = "layout-b";
function rB(input){
  const e=input.chatReq,q=input.turnstileToken,p=input.proofToken;
  return {chatReq:e,turnstileToken:q,proofToken:p,extra:true};
}
;initB=e((()=>{const state={ready:true};return state}));
export{rB as renamedHelperB,initB as renamedInitializerB};
