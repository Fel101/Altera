const test=require('node:test'),assert=require('node:assert/strict'),fixture=require('./helpers/main-page.cjs');
const gate=()=>{let resolve;return{promise:new Promise(yes=>{resolve=yes;}),resolve:value=>resolve(value)};};
async function until(check){const end=performance.now()+2000;while(performance.now()<end){if(check())return;await new Promise(resolve=>setImmediate(resolve));}assert.fail('Expected main-page state');}

test('an older model list cannot overwrite a newer provider selection',async()=>{
 const g=gate();let waiting=false;const f=fixture({intercept(call){if(call.url.includes('/lmstudio/models')){waiting=true;return{ok:true,json:()=>g.promise};}}}),older=f.context.refreshModels();await until(()=>waiting);
 f.nodes.get('providerSelect').value='custom';await f.context.onProviderChange();g.resolve({models:['Old provider model']});await older;assert.equal(f.run('currentProviderKey'),'custom');assert.equal(f.nodes.get('modelSelect').value,'synthetic-model');
});

test('a delayed custom configuration save cannot switch to a newly selected mutable provider',async()=>{
 const g=gate();let waiting=false;const f=fixture({intercept(call){if(call.url==='/api/llm/config'){waiting=true;return{ok:true,json:()=>g.promise};}}});f.run("currentProviderKey='custom';currentModel='original-model';");
 f.nodes.get('customBaseUrl').value='http://synthetic.invalid/v1';f.nodes.get('customApiKey').value='synthetic-api-secret';const applying=f.context.applyProviderModel();await until(()=>waiting);
 f.nodes.get('providerSelect').value='lmstudio';await f.context.onProviderChange();g.resolve({});await applying;assert.equal(f.calls.some(call=>call.url==='/api/llm/switch'),false);assert.equal(f.calls[0].body.provider,'custom');
});

test('workspace reads cannot erase newer text typed while their results were pending',async()=>{
 const g=gate();let waiting=false;const f=fixture({intercept(call){if(call.url.startsWith('/api/tools/file?')){waiting=true;return{ok:true,json:()=>g.promise};}}});f.nodes.get('workspaceFilePath').value='synthetic.txt';const reading=f.context.readWorkspaceFile();await until(()=>waiting);f.nodes.get('workspaceFileContent').value='Newer unsaved file text';
 g.resolve({path:'synthetic.txt',content:'Older disk text'});await reading;assert.equal(f.nodes.get('workspaceFileContent').value,'Newer unsaved file text');assert.match(f.nodes.get('workspaceRootLabel').textContent,/Newer file edits/);
});



test('a conversation starter preserves existing text and never submits into a later conversation',async()=>{
 const f=fixture();f.nodes.get('messageInput').value='Existing draft';await f.context.useStarter({textContent:'Synthetic starter'});assert.equal(f.nodes.get('messageInput').value,'Existing draft');assert.equal(f.timers.size,0);
 f.nodes.get('messageInput').value='';await f.context.useStarter({textContent:'Synthetic starter'});f.run('currentSessionId=2;');await f.fireTimer(300);assert.equal(f.submitted,0);
});

test('workspace file names become text and event data, never executable inline handlers',async()=>{
 const path="synthetic');alert('not code')//.txt",f=fixture({intercept(call){if(call.url==='/api/tools/files')return{ok:true,json:async()=>({files:[{path}]})};}});await f.context.refreshWorkspaceFiles(false);const chip=f.nodes.get('workspaceFileList').children[0];assert.equal(chip.textContent,path);assert.equal(chip.getAttribute('onclick'),null);
});

test('choosing a provider with no listed models never carries over the previous provider model',async()=>{
 const f=fixture({intercept(call){if(call.url.includes('/models'))return{ok:true,json:async()=>({models:[]})};}});f.run("currentModel='previous-private-model';");f.nodes.get('providerSelect').value='custom';await f.context.onProviderChange();assert.equal(f.run('currentModel'),'');
});

test('ordinary browser exit warns for unfinished workspace text without pretending to save it',()=>{
 const f=fixture();f.context.setupMainEditTracking();f.nodes.get('workspaceFileContent').value='Synthetic unfinished workspace edit';assert.equal(f.unload(),true);
});

test('an acknowledged custom configuration save clears only the saved credential exit warning',async()=>{
 const f=fixture({intercept(call){
  if(call.url==='/api/llm/providers')return{ok:true,json:async()=>({providers:[{key:'custom',name:'Synthetic custom',is_active:true}],active_provider:'custom',active_model:'synthetic-model'})};
  if(call.url==='/api/llm/switch')return{ok:true,json:async()=>({status:'ok',provider:'custom',model:'synthetic-model'})};
 }});f.context.setupMainEditTracking();await f.context.loadProviderSettings();f.nodes.get('customApiKey').value='synthetic-saved-key';await f.nodes.get('customApiKey').fire('input');assert.equal(f.unload(),true);
 await f.context.applyProviderModel();await until(()=>f.unload()===false);assert.equal(f.calls.find(call=>call.url==='/api/llm/config').body.api_key,'synthetic-saved-key');
});
