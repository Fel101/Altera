const test = require('node:test');
const assert = require('node:assert/strict');
const {createAlteraPage, createAlteraComponent, adoptLegacyDatabase} = require('../static/js/browser-state.js');
const tick = () => new Promise(resolve => setImmediate(resolve));

function doc(metadata = {}) {
    class Node {
        constructor(tag) { this.tagName=tag; this.children=[]; this.dataset={}; this.listeners={}; this.classList={toggle() {}}; }
        append(...nodes) { for (const node of nodes) { node.parent=this; this.children.push(node); } }
        prepend(node) { node.parent=this; this.children.unshift(node); }
        replaceChildren(...nodes) { this.children=[]; this.append(...nodes); }
        setAttribute(name,value) { this[name]=value; }
        addEventListener(name,fn) { this.listeners[name]=fn; }
        remove() { if(this.parent) this.parent.children=this.parent.children.filter(node=>node!==this); }
    }
    const listeners=[], events={};
    const d={readyState:'loading', documentElement:new Node('html'),head:new Node('head'),body:new Node('body'),title:'private fixture',
        createElement:tag=>new Node(tag),addEventListener(name,fn) {(events[name] ||= []).push(fn);if(name==='DOMContentLoaded') listeners.push(fn);},
        querySelectorAll(selector) {const value=metadata[selector.match(/name="([^"]+)"/)[1]];return value===undefined?[]:[{content:value}];},
        getElementById(id) {const visit=node=>node.id===id?node:node.children.map(visit).find(Boolean);return visit(d.body)||null;}};
    d.emit=(name,event={})=>{for(const fn of events[name]||[])fn(event);};
    d.fire=()=>{d.readyState='complete';for(const fn of listeners.splice(0))fn();};
    return d;
}
function nativeStorage(seed={}) {
    const values=new Map(Object.entries(seed)),reads=[],writes=[];
    return {values,reads,writes,getItem(key){reads.push(key);return values.get(key)??null;},
        setItem(key,value){writes.push(key);values.set(key,String(value));},removeItem(key){writes.push(key);values.delete(key);}};
}
function setup() {
    const document=doc(), local=nativeStorage({altera_theme:'saved'}), session=nativeStorage({altera_pending_turns:'original'});
    const page=createAlteraPage({window:{document,localStorage:local,sessionStorage:session,console:{error(){}}},document});
    return {page,document,local,session};
}

test('ordinary storage delegates without scanning or changing the native Storage objects', async()=>{
    const f=setup();
    assert.equal(f.page.storage.local.getItem('altera_theme'),'saved');
    f.page.storage.session.setItem('altera_pending_turns','kept');assert.equal(f.session.values.get('altera_pending_turns'),'kept');
    let initialized=false;f.page.ready(()=>{initialized=true;});await tick();assert.equal(initialized,false);
    f.document.fire();await tick();assert.equal(initialized,true);
    assert.equal(f.local.values.get('altera_theme'),'saved');
});

test('settings saved under the old her_ names move to altera_ names once', () => {
    const local=nativeStorage({her_theme:'light',her_accent:'#112233',altera_room:'night',her_room:'day',unrelated:'x'});
    const session=nativeStorage({her_composer_draft:'{"text":"draft"}'});
    createAlteraPage({window:{localStorage:local,sessionStorage:session},document:doc({})});
    assert.equal(local.values.get('altera_theme'),'light');assert.equal(local.values.get('altera_accent'),'#112233');
    assert.equal(local.values.get('altera_room'),'night','an existing new value wins');
    assert.equal(session.values.get('altera_composer_draft'),'{"text":"draft"}');
    assert.deepEqual([...local.values.keys()].filter(key=>key.startsWith('her_')),[]);
    assert.equal(session.values.has('her_composer_draft'),false);assert.equal(local.values.get('unrelated'),'x');
    const blocked={get localStorage(){throw Error('storage off');},get sessionStorage(){throw Error('storage off');}};
    assert.doesNotThrow(()=>createAlteraPage({window:blocked,document:doc({})}));
});

function fakeDatabases(seed) {
    // name -> {storeName: {keyPath, next, rows: Map}}; just enough IndexedDB for adoptLegacyDatabase.
    const all=new Map(Object.entries(seed)),deleted=[];
    const answer=(fn)=>{const r={};queueMicrotask(()=>{try{r.result=fn();r.onsuccess?.();}catch(error){r.error=error;r.onerror?.();}});return r;};
    const open=stores=>({closed:false,objectStoreNames:{contains:name=>name in stores},close(){this.closed=true;},
        transaction(name){const s=stores[name],tx={};setImmediate(()=>tx.oncomplete?.());
            tx.objectStore=()=>({keyPath:s.keyPath,getAll:()=>answer(()=>[...s.rows.values()]),
                add(row){const r={};queueMicrotask(()=>{const key=row[s.keyPath]??++s.next;
                    if(s.rows.has(key))r.onerror?.({preventDefault(){}});else{s.rows.set(key,{...row,[s.keyPath]:key});r.onsuccess?.();}});return r;}});
            return tx;}});
    return {all,deleted,api:{databases:async()=>[...all.keys()].map(name=>({name})),
        open(name){const r=answer(()=>open(all.get(name)));return r;},deleteDatabase(name){deleted.push(name);all.delete(name);}}};
}

test('a browser database under its old her- name is copied into the new one, then deleted', async()=>{
    const queue=(rows,next)=>({'pending-messages':{keyPath:'id',next,rows:new Map(rows.map(row=>[row.id,row]))}});
    const f=fakeDatabases({'her-offline-queue':queue([{id:1,message:'old one'},{id:2,message:'old two'}],2),
        'altera-offline-queue':queue([{id:1,message:'new'}],1)});
    const db=await new Promise(resolve=>{const r=f.api.open('altera-offline-queue');r.onsuccess=()=>resolve(r.result);});
    await adoptLegacyDatabase(f.api,'her-offline-queue',db,'pending-messages',{renumber:true});
    const rows=[...f.all.get('altera-offline-queue')['pending-messages'].rows.values()].map(row=>row.message);
    assert.deepEqual(rows,['new','old one','old two']);assert.deepEqual(f.deleted,['her-offline-queue']);
    await adoptLegacyDatabase(f.api,'her-offline-queue',db,'pending-messages',{renumber:true});
    assert.deepEqual(f.deleted,['her-offline-queue'],'nothing left to move the second time');
});
