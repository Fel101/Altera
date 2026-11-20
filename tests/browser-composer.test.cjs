const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const {webcrypto}=require('node:crypto');
const source=fs.readFileSync('static/js/app.js','utf8');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const deferred=()=>{let resolve;const promise=new Promise(yes=>{resolve=yes;});return {promise,resolve};};
function fixture({storeError=false}={}){
    const input={value:'original text'},values=new Map(),appended=[],requests=[],notices=[];
    const context=vm.createContext({crypto:webcrypto,AlteraAPI:{epoch:'epoch'},currentSessionId:1,isStreaming:false,
        recordingStarting:false,isRecording:false,voiceTranscribing:false,
        AlteraStorage:{session:{getItem:key=>values.get(key)??null,setItem(key,value){if(storeError)throw Error('Browser storage unavailable');values.set(key,value);},removeItem:key=>values.delete(key)}},
        document:{getElementById:id=>id==='messageInput'?input:null},console:{error(){}},
        stopSpeech(){},showTypingIndicator(){},hideTypingIndicator(){},updateSendButton(){},autoResize(){},
        appendMessage:(role,text)=>appended.push({role,text})});
    vm.runInContext(source.slice(source.indexOf('async function sendMessage('),source.indexOf('function pendingTurns(')),context);
    vm.runInContext(source.slice(source.indexOf('function pendingTurns('),source.indexOf('// Message UI Helpers')),context);
    context.showReplyNotice=text=>notices.push(text);
    context.readChatStream=async(payload)=>{requests.push(payload);return 'saved';};
    return {context,input,values,appended,requests,notices,send:()=>context.sendMessage({preventDefault(){}})};
}
test('ordinary chat retains its existing behavior when optional browser storage is blocked',async()=>{
    const f=fixture({storeError:true});await f.send();
    assert.equal(f.requests.length,1);assert.equal(f.input.value,'');assert.equal(f.appended.length,1);
});
