const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');
const tick = () => new Promise(resolve => setImmediate(resolve));
const defer = () => {let resolve, reject; const promise = new Promise((a,b) => {resolve=a; reject=b;}); return {promise,resolve,reject};};

function fixture(options = {}) {
    const nodes = new Map(), documentEvents = {}, windowEvents = {}, calls = [], reviews = [], messages = [];
    class Node {
        constructor(id) {this.id=id;this.value='';this.files=[];this.checked=false;this.disabled=false;this.open=false;this.events={};this.children=[];this.dataset={};this.textContent='';}
        addEventListener(name, fn) {this.events[name]=fn;}
        showModal() {this.open=true;}
        close() {this.open=false;}
        append(node) {this.children.push(node);}
        reset() {for (const id of ['Text','File','Self','Providers']) {nodes.get('sharedScene'+id).value='';nodes.get('sharedScene'+id).checked=false;nodes.get('sharedScene'+id).files=[];}}
    }
    for (const name of ['Open','Dialog','Form','Title','Help','Context','Recipients','File','Text','Self','Providers','Status','Submit','Discard','Close']) nodes.set('sharedScene'+name,new Node(name));
    const document = {getElementById:id=>nodes.get(id), addEventListener:(name,fn)=>{documentEvents[name]=fn;},
        createElement:tag=>tag==='canvas'?{width:0,height:0,getContext:()=>({drawImage(){}}),toBlob:fn=>fn(new Blob(['synthetic png'],{type:'image/png'}))}:new Node(tag)};
    class Reader {readAsDataURL(){this.result='data:image/png;base64,c3ludGhldGlj';queueMicrotask(()=>this.onload());}}
    const ctx=vm.createContext({document, Blob, AbortController, FileReader:Reader, crypto:webcrypto, setTimeout, clearTimeout,
        currentSessionId:1, Companion:{active:{id:'mara',name:'Mara'},selection:1}, AlteraAPI:{epoch:'epoch-one'},
        createImageBitmap:async()=>options.bitmap ? options.bitmap.promise : {width:2,height:2,close(){}},
        fetch:async(url,init)=>{if(url.endsWith('/context')) {reviews.push({url,init,body:JSON.parse(init.body)});
                return options.review ? options.review(url,init) : {ok:true,json:async()=>({vision_endpoint:'http://vision.test/v1',
                    vision_model:'vision-fixture',image_endpoint:'http://image.test',provider_fingerprint:'a'.repeat(64)})};}
            calls.push({url,init,body:JSON.parse(init.body)});return options.fetch ? options.fetch(url,init,calls.length) :
                {ok:true,json:async()=>({id:'p-1234567890',message_id:7,avatar_id:'mara',session_id:1})};},
        appendMessage:()=>{const content=new Node('content'),message={querySelector:()=>content};messages.push(content);return message;},
        hydratePhotos(){},showReplyNotice(){}, confirm:()=>true,
        addEventListener:(name,fn)=>{windowEvents[name]=fn;}});
    ctx.window=ctx;
    require('./helpers/ordinary-page.cjs')(ctx);
    vm.runInContext(fs.readFileSync('static/js/shared-scene.js','utf8'),ctx);
    documentEvents.DOMContentLoaded();
    const file=nodes.get('sharedSceneFile'), text=nodes.get('sharedSceneText'), self=nodes.get('sharedSceneSelf'), providers=nodes.get('sharedSceneProviders');
    return {ctx,nodes,calls,reviews,messages,windowEvents,file,text,self,providers,
        fill(){file.files=[{size:100,type:'image/jpeg'}];text.value='A synthetic garden';self.checked=true;providers.checked=true;},
        async submit(){await tick();nodes.get('sharedSceneForm').events.submit({preventDefault(){}});await tick();await tick();},
        async finish(){for(let i=0;i<8;i++)await tick();}};
}

test('explicit confirmation sends one bounded image to the captured conversation',async()=>{
    const f=fixture();f.ctx.AlteraSharedScene.open();f.fill();await f.submit();
    assert.equal(f.calls.length,1);const body=f.calls[0].body;
    assert.equal(body.session_id,1);assert.equal(body.avatar_id,'mara');assert.equal(body.dataset_epoch,'epoch-one');
    assert.equal(body.self_reference,true);assert.equal(body.adult_confirmation,true);assert.equal(body.provider_confirmation,true);
    assert.equal(body.provider_fingerprint,'a'.repeat(64));
    assert.match(f.nodes.get('sharedSceneRecipients').textContent,/http:\/\/vision\.test\/v1/);
    assert.match(f.nodes.get('sharedSceneRecipients').textContent,/http:\/\/image\.test/);
    assert.equal(body.reference_png,'c3ludGhldGlj');assert.match(body.request_id,/^shared_[0-9a-f]{32}$/);
    assert.equal(f.messages.length,1);assert.equal(f.messages[0].children[0].dataset.photoId,'p-1234567890');
    assert.equal(f.nodes.get('sharedSceneDialog').open,false);
});

test('an uncertain response keeps the same request and photo for an explicit retry',async()=>{
    const f=fixture({fetch:async(_url,_init,n)=>{if(n===1)throw Error('connection lost');
        return {ok:true,json:async()=>({id:'p-1234567890',message_id:7,avatar_id:'mara',session_id:1})};}});
    f.ctx.AlteraSharedScene.open();f.fill();await f.submit();
    assert.equal(f.nodes.get('sharedSceneText').disabled,true);assert.match(f.nodes.get('sharedSceneStatus').textContent,/Retry the same request/);
    await f.submit();assert.equal(f.calls.length,2);assert.deepEqual(f.calls[1].body,f.calls[0].body);assert.equal(f.messages.length,1);
});

test('switching companions is blocked until the original draft is explicitly discarded',async()=>{
    const f=fixture();f.ctx.AlteraSharedScene.open();f.fill();
    assert.equal(f.ctx.AlteraSharedScene.prepareNavigation(),false);assert.match(f.nodes.get('sharedSceneStatus').textContent,/Finish or discard/);
    f.nodes.get('sharedSceneDiscard').events.click();assert.equal(f.ctx.AlteraSharedScene.prepareNavigation(),true);
    assert.equal(f.file.files.length,0);assert.equal(f.calls.length,0);
});

test('unconfirmed, oversized and changed-context requests do not upload',async()=>{
    const f=fixture();f.ctx.AlteraSharedScene.open();f.fill();f.self.checked=false;await f.submit();assert.equal(f.calls.length,0);
    f.self.checked=true;f.file.files=[{size:9*1024*1024,type:'image/jpeg'}];await f.submit();assert.equal(f.calls.length,0);
    f.file.files=[{size:100,type:'image/jpeg'}];f.ctx.currentSessionId=2;await f.submit();assert.equal(f.calls.length,0);
});

test('an unresolved recipient review never submits an image',async()=>{
    const review=defer(),f=fixture({review:()=>review.promise});f.ctx.AlteraSharedScene.open();f.fill();
    await f.submit();assert.equal(f.calls.length,0);assert.equal(f.nodes.get('sharedSceneSubmit').disabled,true);
    review.resolve({ok:true,json:async()=>({vision_endpoint:'http://vision.test',image_endpoint:'http://image.test',
        vision_model:'fixture',provider_fingerprint:'a'.repeat(64)})});await f.finish();
    assert.equal(f.calls.length,0);
});
