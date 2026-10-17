"""Synthetic local files/pipes only; never calls real Site tools or HTTP."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import py_compile
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest

from academic_radar import native_spool as legacy

PROJECT = Path(__file__).resolve().parents[1]
LAUNCHER = PROJECT/'scripts/d1-native-relay.py'
PUMP = PROJECT/'scripts/d1-native-pump.js'
spec = importlib.util.spec_from_file_location('synthetic_d1_relay', LAUNCHER)
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)

NODE = r'''
const fs=require('node:fs'),vm=require('node:vm'),cp=require('node:child_process');
const options=JSON.parse(process.argv[1]),mode=process.argv[2];let calls=0;
let maxCommand=0,appendBatches=0,pendingReply=null,execCalls=0,pollCalls=0,sessionOutput='';
const saved=new Map();let clockOffset=0;
const pendingExact=()=>!!(pendingReply&&pendingReply.number===1&&pendingReply.value.ok===true&&pendingReply.value.result.padding==='x'.repeat(365000));
const ctx={Date:class extends Date{static now(){return Date.now()+clockOffset;}},store:(key,value)=>{saved.set(key,value);if(key.startsWith('d1_pending_reply:'))pendingReply=value;},load:key=>saved.get(key),tools:{
write_stdin:async args=>{
  pollCalls++;
  if(args.session_id!==4321||args.chars!=='')throw Error('WRONG_SESSION_OR_INPUT');
  if(mode==='session_exception')throw Error('PRIVATE_WAIT_EXCEPTION_SENTINEL');
  if(mode==='session_running')return {session_id:4321,output:''};
  if(mode==='session_changed')return {session_id:4322,output:''};
  if(mode==='session_poll_contradiction')return {session_id:4321,exit_code:0,output:sessionOutput.slice(3)};
  if(mode==='session_oversize')return {session_id:4321,output:'PRIVATE_OUTPUT_SENTINEL'+'x'.repeat(2000000)};
  if(pollCalls===1)return {session_id:4321,output:sessionOutput.slice(3,13)};
  return {exit_code:mode==='session_nonzero'?17:0,output:sessionOutput.slice(13)};
},exec_command:async args=>{
execCalls++;
maxCommand=Math.max(maxCommand,Buffer.byteLength(args.cmd));
if(Buffer.byteLength(args.cmd)>98304)throw Error('COMMAND_TOO_LARGE');
if(args.cmd.startsWith('set -e;')){
  appendBatches++;
  if(mode==='exec_throw')throw Error('PRIVATE_LOCAL_ARGV_SENTINEL');
  if(mode==='batch_failure'){
    const at=args.cmd.indexOf('_d1_output=$(',args.cmd.indexOf('_d1_output=$(')+1);
    if(at!==-1)args.cmd=args.cmd.slice(0,at)+'exit 17;\n'+args.cmd.slice(at);
  }
  if(mode==='bad_progress')args.cmd=args.cmd.replace('test "$_d1_output"', '_d1_output=INVALID; test "$_d1_output"');
}
const r=cp.spawnSync('/bin/sh',['-c',args.cmd],{encoding:'utf8',maxBuffer:3000000});
if(mode==='expired_preflight' && args.cmd.includes("'preflight'") && r.status===0){const value=JSON.parse(r.stdout);value.deadline_unix_ms=Date.now()-1;r.stdout=JSON.stringify(value);}
if(mode.startsWith('session_')&&args.cmd.startsWith('set -e;')&&appendBatches===1){
  if(mode==='session_deadline')clockOffset=116000;
  sessionOutput=r.stdout;
  if(mode==='session_initial_contradiction')return {session_id:4321,exit_code:0,output:sessionOutput};
  return {session_id:4321,output:sessionOutput.slice(0,3)};
}
return {exit_code:r.status,output:r.stdout};
},mcp__codex_apps__app_read_main_store:async args=>{calls++;if(args.action!=='head')throw Error('wrong action');return {structuredContent:{head:{revision:0,import_id:null,manifest_sha256:null,watermarks:{feedback_cursor:0,profile_cursor:0,request_id:0}}}};},
mcp__codex_apps__app_write_main_store:async args=>{calls++;if(mode==='reject')throw Error('PRIVATE_REJECTION_SENTINEL');if(args.data.chunks[0].data_base64!=='x'.repeat(500000))throw Error('payload mismatch');return {content:[{type:'text',text:JSON.stringify({ok:true,padding:mode==='ascii'||mode==='batch_failure'||mode==='bad_progress'||mode==='exec_throw'||mode.startsWith('session_')?'x'.repeat(365000):mode==='special'?('\\\"\'\n\t\u0000😀中文'.repeat(25000)):'中文😀'.repeat(60000)})}]};}}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.env.PUMP,'utf8'),ctx);
const diagnostic=()=>saved.get('d1_pending_local_exec:'+options.spool)??null;
ctx.pumpD1(options).then(result=>process.stdout.write(JSON.stringify({result,calls,maxCommand,appendBatches,pendingExact:pendingExact(),execCalls,pollCalls,diagnostic:diagnostic()})+'\n')).catch(async error=>{
  let blocked=null,unchanged=true;
  if(mode.startsWith('session_')){const before=[execCalls,pollCalls,calls];try{await ctx.pumpD1(options);}catch(e){blocked=e.message;}unchanged=JSON.stringify(before)===JSON.stringify([execCalls,pollCalls,calls]);}
  process.stdout.write(JSON.stringify({error:error.message,calls,maxCommand,appendBatches,pendingExact:pendingExact(),execCalls,pollCalls,diagnostic:diagnostic(),blocked,unchanged})+'\n');
});
'''


@unittest.skipUnless(os.name == 'posix', 'Private relay requires POSIX directory descriptors/process groups')
class D1NativeSpoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = self.root/'source.json'
        pin = relay.pin_source(self.manifest)
        self.sha = pin['source_manifest_sha256']
        self.files = relay.verify_source(self.manifest, self.sha)
        self.transport = relay.make_transport(self.manifest, self.sha, self.files)

    def command(self, *arguments, launcher=LAUNCHER, manifest=None, sha=None):
        return [sys.executable, '-I', '-B', str(launcher), '--source-manifest', str(manifest or self.manifest),
                '--source-manifest-sha256', sha or self.sha, *map(str, arguments)]

    def io(self, action, spool, *extra):
        return relay.io_main(self.transport, [action, '--spool', str(spool.path), '--number', '1', *extra])

    def pump(self, spool, mode, *, launcher=LAUNCHER, manifest=None, sha=None, pump=PUMP):
        if shutil.which('node') is None:
            self.skipTest('Node is required for the synthetic host-pump fixture')
        options = {'python':sys.executable, 'launcher':str(launcher), 'workspace':str(self.root),
                   'spool':str(spool), 'source_manifest':str(manifest or self.manifest), 'source_manifest_sha256':sha or self.sha,
                   'start':1, 'max_requests':1,
                   'operations':['site.read_main_store' if mode == 'status' else 'site.write_main_store']}
        env = {**os.environ, 'PUMP':str(pump)}
        out = subprocess.run(['node','-e',NODE,json.dumps(options),mode], capture_output=True,text=True,env=env,timeout=60)
        self.assertEqual(out.returncode,0,out.stderr)
        self.assertLess(len(out.stdout),5000)
        self.assertNotIn('PRIVATE_REJECTION_SENTINEL',out.stdout+out.stderr)
        self.assertNotIn('PRIVATE_LOCAL_ARGV_SENTINEL',out.stdout+out.stderr)
        self.assertNotIn('PRIVATE_WAIT_EXCEPTION_SENTINEL',out.stdout+out.stderr)
        self.assertNotIn('PRIVATE_OUTPUT_SENTINEL',out.stdout+out.stderr)
        return json.loads(out.stdout)

    def stop(self, process):
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)
        if process.stdout: process.stdout.close()
        if process.stderr: process.stderr.close()

    def test_real_cli_status_with_synthetic_tool_result(self):
        p = subprocess.Popen(self.command('run','--workspace',self.root,'--spool','status','--timeout-seconds','30','--','status'),
                             stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.addCleanup(self.stop,p)
        ready = json.loads(p.stdout.readline())
        self.assertEqual(ready['operation'],'site.read_main_store')
        out = self.pump(self.root/'status','status')
        self.assertEqual(out['calls'],1)
        self.assertTrue(json.loads(p.stdout.readline())['ok'])
        self.assertEqual(p.wait(timeout=5),0)
        self.assertEqual(p.stderr.read(),'')
        result=json.loads((self.root/'status/result.json').read_text())
        self.assertEqual(result['result']['head']['revision'],0)

    def make_pending(self, operation='site.write_main_store', arguments=None, deadline=None):
        spool=self.transport.PrivateSpool(self.root,'opaque')
        self.addCleanup(spool.close)
        req={'protocol':self.transport.PROTOCOL,'type':'request','session_id':'a'*32,'request_id':1,
             'operation':operation,'arguments':arguments or {'action':'put_chunks','data':{
                 'chunks':[{'sha256':'b'*64,'data_base64':'x'*500000}]}}}
        raw=self.transport._encode(req)
        spool.save('request-1.json',raw)
        spool.save('live-1.json',b'{}\n')
        spool.save('pending-1.json',self.transport._encode({'heartbeat':'live-1.json',
            'deadline_unix_ms':int((time.time()+60)*1000) if deadline is None else deadline,
            'request_sha256':hashlib.sha256(raw).hexdigest()}))
        return spool,req

    def test_large_unicode_payload_is_opaque_exact_and_measured(self):
        spool,req=self.make_pending()
        out=self.pump(spool.path,'large')
        self.assertEqual(out['calls'],1)
        self.assertEqual(out['result']['metrics']['native_calls'],1)
        self.assertGreater(out['result']['metrics']['local_exec_calls'],1)
        value=self.transport._decode(spool.response(1))
        self.assertEqual(value['result']['padding'],'中文😀'*60000)
        self.assertEqual(value['operation'],req['operation'])
        self.assertGreater(out['result']['request_bytes'],500000)
        self.assertGreater(out['result']['response_bytes'],600000)
        self.assertTrue((spool.path/'invoke-1.json').exists())


    def test_ascii_response_batches_reduce_exec_roundtrips(self):
        spool,req=self.make_pending()
        out=self.pump(spool.path,'ascii')
        self.assertEqual(out['calls'],1)
        self.assertEqual(self.transport._decode(spool.response(1))['result']['padding'],'x'*365000)
        self.assertLessEqual(out['maxCommand'],98304)
        self.assertLessEqual(out['appendBatches'],5)
        self.assertLessEqual(out['result']['metrics']['local_exec_calls'],14)
        self.assertEqual(out['maxCommand'],out['result']['metrics']['max_local_exec_command_bytes'])

    def test_quoted_control_unicode_response_roundtrips_under_command_cap(self):
        spool,req=self.make_pending()
        out=self.pump(spool.path,'special')
        self.assertEqual(out['calls'],1)
        value=self.transport._decode(spool.response(1))
        self.assertEqual(value['result']['padding'], '\\\"\'\n\t\x00😀中文'*25000)
        self.assertLessEqual(out['maxCommand'],98304)

    def test_partial_batch_failure_never_repeats_native_call(self):
        spool,req=self.make_pending()
        out=self.pump(spool.path,'batch_failure')
        self.assertEqual(out['calls'],1)
        self.assertEqual(out['error'],'local_spool_io_failed')
        self.assertTrue(out['pendingExact'])
        self.assertFalse((spool.path/'response-1.json').exists())
        self.assertGreater((spool.path/'response-1.partial').stat().st_size,0)
        self.assertLess((spool.path/'response-1.partial').stat().st_size,32768)
        self.assertEqual(self.pump(spool.path,'batch_failure')['calls'],0)

    def test_thrown_local_error_is_redacted_and_retains_exact_reply(self):
        spool,req=self.make_pending()
        out=self.pump(spool.path,'exec_throw')
        self.assertEqual(out['calls'],1)
        self.assertEqual(out['error'],'local_spool_io_failed')
        self.assertTrue(out['pendingExact'])
        self.assertFalse((spool.path/'response-1.json').exists())
        self.assertTrue((spool.path/'invoke-1.json').exists())
        self.assertEqual(self.pump(spool.path,'exec_throw')['calls'],0)

    def test_wrong_intermediate_progress_stops_before_next_append(self):
        spool,req=self.make_pending()
        out=self.pump(spool.path,'bad_progress')
        self.assertEqual(out['calls'],1)
        self.assertEqual(out['error'],'local_spool_io_failed')
        self.assertTrue(out['pendingExact'])
        self.assertFalse((spool.path/'response-1.json').exists())
        self.assertGreater((spool.path/'response-1.partial').stat().st_size,0)
        self.assertLess((spool.path/'response-1.partial').stat().st_size,32768)
        self.assertEqual(self.pump(spool.path,'bad_progress')['calls'],0)

    def test_replacement_pump_works_with_separately_pinned_original_helper(self):
        root=self.checkout_copy(); manifest=self.root/'copy-source.json'
        helper_pump=root/'scripts/d1-native-pump.js'
        # Simulate an independently pinned helper release; its Python is identical.
        helper_pump.write_bytes(helper_pump.read_bytes()+b'// Independent helper release fixture.\n')
        sha=relay.pin_source(manifest,root)['source_manifest_sha256']
        self.assertNotEqual(sha,self.sha)
        spool,req=self.make_pending()
        out=self.pump(spool.path,'ascii',launcher=root/'scripts/d1-native-relay.py',manifest=manifest,sha=sha)
        self.assertEqual(out['calls'],1)
        self.assertEqual(self.transport._decode(spool.response(1))['result']['padding'],'x'*365000)
        self.assertEqual(relay.verify_source(manifest,sha,root)['scripts/d1-native-pump.js'],hashlib.sha256(helper_pump.read_bytes()).hexdigest())

    def test_yielded_local_exec_polls_same_session_to_exact_success(self):
        spool,_=self.make_pending()
        out=self.pump(spool.path,'session_success')
        self.assertEqual(out['calls'],1);self.assertEqual(out['pollCalls'],2)
        self.assertEqual(out['appendBatches'],5)
        self.assertIsNone(out['diagnostic'])
        self.assertEqual(self.transport._decode(spool.response(1))['result']['padding'],'x'*365000)
        metrics=out['result']['metrics']
        self.assertEqual(metrics['local_exec_calls'],out['execCalls'])
        self.assertEqual(metrics['local_poll_calls'],2)
        self.assertEqual(metrics['local_bridge_calls'],out['execCalls']+2)

    def session_failure(self,mode,stage,polls):
        spool,_=self.make_pending()
        out=self.pump(spool.path,mode)
        self.assertEqual(out['calls'],1);self.assertEqual(out['appendBatches'],1)
        self.assertEqual(out['pollCalls'],polls);self.assertTrue(out['pendingExact'])
        self.assertEqual(out['blocked'],'unreconciled_local_spool_session');self.assertTrue(out['unchanged'])
        info=out['diagnostic'];self.assertEqual(info['stage'],stage)
        self.assertEqual(info['session_id'],4321);self.assertEqual(info['action'],'append_batch')
        self.assertGreater(info['counters']['native_request_json_bytes'],500000)
        self.assertGreater(info['counters']['native_result_json_bytes'],365000)
        self.assertEqual(info['request_id'],1);self.assertEqual(info['offset_start'],0)
        self.assertGreater(info['offset_end'],0);self.assertLessEqual(info['command_bytes'],98304)
        self.assertEqual(info['counters']['native_calls'],1)
        self.assertEqual(info['counters']['local_exec_calls'],out['execCalls'])
        self.assertFalse((spool.path/'response-1.json').exists())
        self.assertFalse({'cmd','argv','stdout','stderr','output','exception'} & set(info))
        return out

    def test_still_running_session_stops_at_fixed_poll_bound(self):
        out=self.session_failure('session_running','bounded_stop',12)
        self.assertEqual(out['error'],'local_spool_session_unresolved')
        self.assertEqual(out['diagnostic']['polls'],12)

    def test_session_wait_is_not_started_below_minimum_remaining_time(self):
        self.session_failure('session_deadline','bounded_stop',0)

    def test_yielded_session_later_nonzero_exit_never_reexecutes(self):
        out=self.session_failure('session_nonzero','terminal',2)
        self.assertEqual(out['diagnostic']['exit_code'],17)

    def test_yielded_session_exception_is_unknown_redacted_and_retained(self):
        out=self.session_failure('session_exception','unknown',1)
        self.assertIsNone(out['diagnostic']['exit_code'])

    def test_changed_poll_session_fails_without_switching_target(self):
        self.session_failure('session_changed','invalid_session',1)

    def test_initial_running_and_terminal_metadata_is_rejected(self):
        self.session_failure('session_initial_contradiction','contradictory_result',0)

    def test_poll_running_and_terminal_metadata_is_rejected(self):
        self.session_failure('session_poll_contradiction','contradictory_result',1)

    def test_yielded_session_output_is_bounded_without_body_logging(self):
        out=self.session_failure('session_oversize','output_limit',1)
        self.assertGreater(out['diagnostic']['output_bytes'],2000000)

    def test_library_capability_is_rejected(self):
        spool,_=self.make_pending('library.create_archive',{'path':'/synthetic/nope'})
        with self.assertRaises(self.transport.RelayError): self.io('claim',spool)
        self.assertFalse((spool.path/'claim-1.json').exists())

    def test_claim_cannot_replay_and_wrong_response_cannot_publish(self):
        spool,req=self.make_pending()
        self.io('claim',spool)
        with self.assertRaises(FileExistsError): self.io('claim',spool)
        self.io('preflight',spool)
        with self.assertRaises(FileExistsError): self.io('preflight',spool)
        response={key:req[key] for key in ('protocol','session_id','request_id','operation')}
        response.update(type='response',ok=True,result={'ok':True},session_id='c'*32)
        self.io('append',spool,'--text-json',json.dumps(json.dumps(response)+'\n'))
        with self.assertRaises(self.transport.RelayError): self.io('publish',spool)
        self.assertFalse((spool.path/'response-1.json').exists())

    def test_oversize_response_rejected_before_write(self):
        spool,_=self.make_pending()
        self.io('claim',spool)
        with self.assertRaises(self.transport.RelayError):
            self.io('append',spool,'--offset','1999999','--text-json','"ab"')
        self.assertFalse((spool.path/'response-1.partial').exists())

    def test_native_uncertainty_is_redacted_counted_and_never_retried(self):
        spool,_=self.make_pending()
        out=self.pump(spool.path,'reject')
        self.assertEqual(out['calls'],1)
        self.assertEqual(out['result']['state'],'stopped_native_rejected_or_uncertain')
        self.assertEqual(out['result']['metrics']['native_calls'],1)
        self.assertFalse(self.transport._decode(spool.response(1))['ok'])
        self.assertEqual(self.pump(spool.path,'reject')['calls'],0)

    def test_preflight_delivered_after_deadline_never_invokes_native(self):
        spool,_=self.make_pending()
        out=self.pump(spool.path,'expired_preflight')
        self.assertEqual(out['error'],'native_preflight_failed'); self.assertEqual(out['calls'],0)
        self.assertTrue((spool.path/'invoke-1.json').exists())
        self.assertFalse((spool.path/'response-1.json').exists())

    def test_deadline_and_stale_heartbeat_fail_closed(self):
        spool,_=self.make_pending(deadline=int((time.time()-1)*1000))
        with self.assertRaisesRegex(self.transport.RelayError,'deadline'): self.io('claim',spool)
        self.assertFalse((spool.path/'claim-1.json').exists())

    def test_stale_heartbeat_and_terminal_prevent_invocation(self):
        spool,req=self.make_pending()
        self.io('claim',spool)
        os.utime(spool.path/'live-1.json',(time.time()-30,time.time()-30))
        with self.assertRaisesRegex(self.transport.RelayError,'live'): self.io('preflight',spool)
        os.utime(spool.path/'live-1.json',None)
        spool.save('result.json',self.transport._encode({
            'protocol':self.transport.PROTOCOL,'type':'relay_error','session_id':req['session_id'],
            'request_id':1,'ok':False,'error':{'code':'interrupted'}}))
        with self.assertRaises(self.transport.RelayError): self.io('preflight',spool)
        self.assertFalse((spool.path/'invoke-1.json').exists())
        self.assertFalse(self.io('poll',spool)['ok'])

    def test_malformed_terminal_cannot_report_success(self):
        spool,_=self.make_pending()
        spool.save('result.json',b'{"ok":true,"request_id":1}\n')
        with self.assertRaises(self.transport.RelayError): self.io('poll',spool)

    def test_adapter_does_not_mutate_legacy_globals(self):
        self.assertIn('library.create_archive',legacy.OPERATIONS)
        self.assertNotIn('site.read_main_store',legacy.OPERATIONS)
        self.assertNotIn('library.create_archive',self.transport.OPERATIONS)
        self.assertIsNot(legacy._Pipe,self.transport._Pipe)

    def test_terminal_keeps_only_closed_error_classifications(self):
        for code in sorted(relay.TERMINAL_ERROR_CODES):
            value={'protocol':self.transport.PROTOCOL,'type':'result','session_id':'a'*32,
                   'request_id':5,'ok':False,'error':{'code':code,
                   'message':'PRIVATE_BODY_SENTINEL','path':'PRIVATE_PATH_SENTINEL'}}
            result=self.transport._curated_result(value,'a'*32,5)
            self.assertEqual(result['error'],{'code':code})
            self.assertNotIn('PRIVATE_',json.dumps(result))
        for code in ('private_payload_as_code', 'PRIVATE_BODY_SENTINEL', None, 3, [], {}):
            value={'protocol':self.transport.PROTOCOL,'type':'result','session_id':'a'*32,
                   'request_id':5,'ok':False,'error':{'code':code}}
            self.assertEqual(self.transport._curated_result(value,'a'*32,5)['error'],
                             {'code':'native_host_failed'})

    def test_terminal_classification_does_not_relax_bindings(self):
        value={'protocol':self.transport.PROTOCOL,'type':'result','session_id':'a'*32,
               'request_id':5,'ok':False,'error':{'code':'operation_failed'}}
        for changes in ({'session_id':'b'*32},{'request_id':4},{'ok':0},
                        {'protocol':'wrong'},{'error':[]},{'extra':'private'}):
            with self.assertRaises(self.transport.RelayError):
                self.transport._curated_result({**value,**changes},'a'*32,5)
        success={key:item for key,item in value.items() if key!='error'}
        success.update(ok=True,result={'status':'synthetic'})
        self.assertEqual(self.transport._curated_result(success,'a'*32,5),success)

    def test_failed_terminal_safe_code_reaches_opaque_pump_without_native_call(self):
        spool,req=self.make_pending()
        spool.save('result.json',self.transport._encode({'protocol':self.transport.PROTOCOL,
            'type':'result','session_id':req['session_id'],'request_id':1,'ok':False,
            'error':{'code':'operation_failed'}}))
        self.assertEqual(self.io('poll',spool)['error_code'],'operation_failed')
        out=self.pump(spool.path,'ascii')
        self.assertEqual(out['calls'],0)
        self.assertEqual(out['result']['state'],'done')
        self.assertFalse(out['result']['ok'])
        self.assertEqual(out['result']['error_code'],'operation_failed')

    def test_verified_pump_loader_and_pin_mismatch(self):
        loaded=subprocess.run(self.command('pump'),capture_output=True,text=True,timeout=10)
        self.assertEqual(loaded.returncode,0,loaded.stderr)
        value=json.loads(loaded.stdout)
        self.assertEqual(value['source'],PUMP.read_text())
        self.assertEqual(value['source_sha256'],hashlib.sha256(PUMP.read_bytes()).hexdigest())
        bad=subprocess.run(self.command('check',sha='0'*64),capture_output=True,text=True,timeout=10)
        self.assertEqual(bad.returncode,1)
        self.assertEqual(bad.stdout,'{"error":"d1_relay_failed"}\n')
        self.assertEqual(bad.stderr,'')

    def test_argparse_does_not_echo_private_payload(self):
        bad=subprocess.run(self.command('io','append','--PRIVATE_PAYLOAD_SENTINEL'),capture_output=True,text=True,timeout=10)
        self.assertNotEqual(bad.returncode,0)
        self.assertNotIn('PRIVATE_PAYLOAD_SENTINEL',bad.stdout+bad.stderr)

    def checkout_copy(self):
        root=self.root/'checkout';(root/'src').mkdir(parents=True)
        shutil.copytree(PROJECT/'src/academic_radar',root/'src/academic_radar',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        (root/'scripts').mkdir()
        shutil.copy2(LAUNCHER,root/'scripts'/LAUNCHER.name)
        shutil.copy2(PUMP,root/'scripts'/PUMP.name)
        return root

    def test_pin_reproducible_outside_checkout_and_rejects_symlink_ancestor(self):
        root=self.checkout_copy()
        other=self.root/'other-source.json'
        self.assertEqual(relay.pin_source(other,root)['source_manifest_sha256'],self.sha)
        with self.assertRaises(relay.RelayError): relay.pin_source(root/'self-reference.json',root)
        link=self.root/'linked';link.symlink_to(root,target_is_directory=True)
        with self.assertRaises((OSError,relay.RelayError)): relay.verify_source(other,self.sha,link)
        with self.assertRaises(FileExistsError): relay.pin_source(other,root)
        (root/'src/academic_radar/native_host.py').write_text('# changed\n')
        with self.assertRaisesRegex(relay.RelayError,'reviewed_source_changed'): relay.verify_source(other,self.sha,root)

    def test_isolated_launcher_ignores_poisoned_cache_src_shadow_and_pythonpath(self):
        root=self.checkout_copy();manifest=self.root/'copy-source.json'
        sha=relay.pin_source(manifest,root)['source_manifest_sha256']
        target=root/'src/academic_radar/d1_native_host.py'
        original=hashlib.sha256(target.read_bytes()).hexdigest()
        poison=self.root/'poison.py';poison.write_text("raise RuntimeError('POISONED_CACHE_SENTINEL')\n")
        cache=Path(importlib.util.cache_from_source(str(target)));cache.parent.mkdir()
        py_compile.compile(str(poison),cfile=str(cache),doraise=True)
        raw=cache.read_bytes();info=target.stat()
        cache.write_bytes(raw[:8]+struct.pack('<II',int(info.st_mtime),info.st_size)+raw[16:])
        (root/'src/argparse.py').write_text("raise RuntimeError('SRC_SHADOW_SENTINEL')\n")
        shadow=self.root/'shadow';shadow.mkdir()
        (shadow/'sitecustomize.py').write_text("raise RuntimeError('PYTHONPATH_SENTINEL')\n")
        cmd=self.command('host','status',launcher=root/'scripts/d1-native-relay.py',manifest=manifest,sha=sha)
        p=subprocess.Popen(cmd,cwd=shadow,env={**os.environ,'PYTHONPATH':str(shadow)},
                           stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.addCleanup(self.stop,p)
        req=json.loads(p.stdout.readline())
        self.assertEqual(req['operation'],'site.read_main_store')
        reply={key:req[key] for key in ('protocol','session_id','request_id','operation')}
        reply.update(type='response',ok=True,result={'head':{'revision':0,'import_id':None,'manifest_sha256':None,
            'watermarks':{'feedback_cursor':0,'profile_cursor':0,'request_id':0}}})
        p.stdin.write(json.dumps(reply)+'\n');p.stdin.close()
        self.assertTrue(json.loads(p.stdout.readline())['ok'])
        self.assertEqual(p.wait(timeout=5),0)
        self.assertEqual(p.stderr.read(),'')
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(),original)

    def test_source_only_finder_rejects_unpinned_namespace(self):
        finder=relay.SourceOnlyFinder(self.files)
        with self.assertRaisesRegex(relay.RelayError,'unpinned_module'):
            finder.find_spec('academic_radar.unreviewed_module')


if __name__=='__main__': unittest.main()
