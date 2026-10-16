"""Synthetic read-only journal recovery; no native service is called."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import unittest

import test_d1_native_spool as base
from academic_radar import d1_package, d1_backup

relay = base.relay


class ReadJournalTests(unittest.TestCase):
    setUp = base.D1NativeSpoolTests.setUp
    command = base.D1NativeSpoolTests.command
    stop = base.D1NativeSpoolTests.stop
    checkout_copy = base.D1NativeSpoolTests.checkout_copy

    def old_run(self, prefix=1):
        old = self.transport.PrivateSpool(self.root, 'old-run')
        self.addCleanup(old.close)
        snapshot = {'format':'academic-radar-site-backup','version':1,'pointers':[], 'generations':[],
                    'excluded_uncommitted_generations':0,'feedback_cursor':0,'profile_cursor':0,
                    'main_head':{'revision':0,'import_id':None}}
        begin = {'snapshot':snapshot,'snapshot_sha256':d1_package.digest(snapshot),
                 'hash_algorithm':'sha256-canonical-json-v1','captured_at':'2020-01-02T03:04:05.678Z'}
        requests=[]
        for number in range(1,prefix+2):
            args={'action':'begin','data':{}} if number==1 else {
                'action':'feedback' if number==2 else 'profiles','data':{'after':0,'until':0}}
            req={'protocol':self.transport.PROTOCOL,'type':'request','session_id':'a'*32,
                 'request_id':number,'operation':'site.read_site_backup','arguments':args}
            requests.append(req)
            raw=self.transport._encode(req); claim={'request_sha256':hashlib.sha256(raw).hexdigest()}
            old.save('request-%d.json'%number,raw)
            old.save('pending-%d.json'%number,self.transport._encode({**claim,'heartbeat':'live-%d.json'%number,'deadline_unix_ms':1}))
            old.save('live-%d.json'%number,b'{}\n');old.save('claim-%d.json'%number,self.transport._encode(claim))
            result={key:req[key] for key in ('protocol','session_id','request_id','operation')}
            result.update(type='response',ok=number<=prefix)
            if number<=prefix:
                result['result']=begin if number==1 else {'events':[],'until':0,'done':True,'next':0}
                old.save('invoke-%d.json'%number,self.transport._encode(claim))
            else:result['error']={'code':'host_action_failed'}
            # The helper canonically rewrites failed replies, including key order.
            partial=(json.dumps(result,separators=(',',':'))+'\n').encode()
            old.save('response-%d.partial'%number,partial)
            old.save('response-%d.json'%number,self.transport._response(partial,req))
        old.save('result.json',self.transport._encode({'protocol':self.transport.PROTOCOL,'type':'result',
            'session_id':'a'*32,'request_id':prefix+1,'ok':False,'error':{'code':'native_host_failed'}}))
        return old,requests,begin

    def seal(self, old, prefix=1):
        path=self.root/'read-journal.json'
        info=relay.seal_journal(self.transport,old.path,prefix,path,{
            'root':str(base.PROJECT),'manifest':str(self.manifest),'sha256':self.sha})
        return path,info

    def change(self,path,raw):
        os.chmod(path,0o600);path.write_bytes(raw);os.chmod(path,0o400)

    def hashes(self,path):
        return {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in path.iterdir()}

    def test_exact_replay_matches_tail_preserves_snapshot_and_originals(self):
        old,requests,begin=self.old_run();before=self.hashes(old.path)
        path,info=self.seal(old)
        journal=relay.ReadJournal(self.transport,path,info['journal_sha256'])
        replay=self.transport._decode(journal.response(dict(requests[0],session_id='b'*32)))
        self.assertEqual(replay['result'],begin);self.assertEqual(replay['session_id'],'b'*32)
        self.assertEqual(info['captured_at'],begin['captured_at'])
        self.assertEqual(info['replayed_reads'],1)
        self.assertIsNone(journal.response(dict(requests[1],session_id='b'*32)))
        self.assertTrue(journal.exhausted)
        with self.assertRaisesRegex(relay.RelayError,'revisited'):
            journal.response(dict(requests[0],session_id='b'*32))
        self.assertEqual(self.hashes(old.path),before)

    def test_mismatched_arguments_or_operation_never_falls_through(self):
        old,requests,_=self.old_run();path,info=self.seal(old)
        journal=relay.ReadJournal(self.transport,path,info['journal_sha256'])
        wrong=dict(requests[0],session_id='b'*32,arguments={'action':'feedback','data':{'after':0,'until':0}})
        with self.assertRaisesRegex(relay.RelayError,'mismatch'):journal.response(wrong)
        with self.assertRaisesRegex(relay.RelayError,'forbidden'):
            journal.response(dict(wrong,operation='site.write_main_store'))
        self.assertEqual(journal.replayed,0)
        journal.response(dict(requests[0],session_id='b'*32))
        tail=dict(requests[1],session_id='b'*32,arguments={'action':'profiles','data':{'after':0,'until':0}})
        with self.assertRaisesRegex(relay.RelayError,'mismatch'):journal.response(tail)
        self.assertFalse(journal.exhausted)

    def test_changed_gap_truncated_error_and_symlink_journals_fail(self):
        for defect in ('changed','gap','truncated','error','symlink'):
            with self.subTest(defect=defect):
                child=ReadJournalTests();child.setUp()
                try:
                    old,requests,_=child.old_run(prefix=2);path,info=child.seal(old,prefix=2)
                    target=old.path/'response-1.json'
                    if defect=='gap':target.unlink()
                    elif defect=='symlink':
                        raw=target.read_bytes();target.unlink();other=child.root/'target.json';other.write_bytes(raw);target.symlink_to(other)
                    else:
                        raw=target.read_bytes()
                        if defect=='truncated':raw=raw[:-1]
                        elif defect=='changed':raw=raw.replace(b'2020-01-02',b'2020-01-03')
                        else:
                            value=child.transport._decode(raw);value.pop('result');value.update(ok=False,error={'code':'host_action_failed'});raw=child.transport._encode(value)
                        child.change(target,raw)
                    with self.assertRaises((relay.RelayError,child.transport.RelayError,OSError)):
                        relay.ReadJournal(child.transport,path,info['journal_sha256'])
                finally:child.doCleanups()

    def test_failed_or_invoked_tail_and_later_files_are_rejected(self):
        for defect in ('invoke','extra','write','terminal','error'):
            with self.subTest(defect=defect):
                child=ReadJournalTests();child.setUp()
                try:
                    old,requests,_=child.old_run()
                    if defect=='invoke':old.save('invoke-2.json', (old.path/'claim-2.json').read_bytes())
                    elif defect=='extra':old.save('request-3.json',(old.path/'request-2.json').read_bytes())
                    else:
                        name='request-1.json' if defect=='write' else 'result.json' if defect=='terminal' else 'response-1.json'
                        value=child.transport._decode((old.path/name).read_bytes())
                        if defect=='write':value.update(operation='site.write_main_store',arguments={'action':'begin_import','data':{'manifest_sha256':'b'*64}})
                        elif defect=='terminal':value['request_id']=1
                        else:value.pop('result');value.update(ok=False,error={'code':'host_action_failed'})
                        child.change(old.path/name,child.transport._encode(value))
                    with self.assertRaises((relay.RelayError,child.transport.RelayError,OSError)):
                        child.seal(old)
                finally:child.doCleanups()

    def test_changed_manifest_pin_and_source_pin_are_rejected(self):
        old,_,_=self.old_run();path,info=self.seal(old)
        with self.assertRaisesRegex(relay.RelayError,'pin_changed'):
            relay.ReadJournal(self.transport,path,'0'*64)
        value=json.loads(path.read_bytes());value['original_source']['sha256']='0'*64
        raw=relay.canonical(value);self.change(path,raw)
        with self.assertRaisesRegex(relay.RelayError,'source_pin_changed'):
            relay.ReadJournal(self.transport,path,hashlib.sha256(raw).hexdigest())

    def test_trusted_old_source_must_use_identical_backup_package(self):
        old,_,_=self.old_run();original=self.checkout_copy()
        module=original/'src/academic_radar/d1_backup.py'
        module.write_bytes(module.read_bytes()+b'\n# Deliberately different synthetic source.\n')
        manifest=self.root/'different-source.json'
        sha=relay.pin_source(manifest,original)['source_manifest_sha256']
        with self.assertRaisesRegex(relay.RelayError,'backup_source_changed'):
            relay.seal_journal(self.transport,old.path,1,self.root/'journal.json',{
                'root':str(original),'manifest':str(manifest),'sha256':sha})

    def test_changed_response_after_load_is_rejected_before_replay(self):
        old,requests,_=self.old_run();path,info=self.seal(old)
        journal=relay.ReadJournal(self.transport,path,info['journal_sha256'])
        target=old.path/'response-1.json'
        self.change(target,target.read_bytes().replace(b'2020-01-02',b'2020-01-03'))
        with self.assertRaisesRegex(relay.RelayError,'journal_changed'):
            journal.response(dict(requests[0],session_id='b'*32))
        self.assertEqual(journal.replayed,0)

    def test_success_terminal_requires_exhausted_prefix_and_matched_tail(self):
        old,_,_=self.old_run();path,info=self.seal(old)
        journal=relay.ReadJournal(self.transport,path,info['journal_sha256'])
        adapted=relay.make_transport(self.manifest,self.sha,self.files,journal=journal)
        terminal={'protocol':self.transport.PROTOCOL,'type':'result','session_id':'b'*32,'request_id':1,'ok':True,'result':{}}
        with self.assertRaisesRegex(relay.RelayError,'incomplete'):adapted._curated_result(terminal,'b'*32,1)

    def test_actual_backup_cli_replays_begin_then_only_announces_uncached_reads(self):
        old,requests,begin=self.old_run();before=self.hashes(old.path);path,info=self.seal(old)
        p=subprocess.Popen(self.command('resume-backup','--workspace',self.root,'--spool','new-run',
            '--timeout-seconds','60','--journal',path,'--journal-sha256',info['journal_sha256'],
            '--output',self.root/'finished-backup'),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.addCleanup(self.stop,p)
        new=self.root/'new-run'
        for number,action in ((2,'feedback'),(3,'profiles')):
            event=json.loads(p.stdout.readline());self.assertEqual(event['request_id'],number)
            self.assertEqual(event['replayed_reads'],1)
            with relay.existing(self.transport,new) as spool:
                req=self.transport._decode((new/('request-%d.json'%number)).read_bytes())
                self.assertEqual(req['arguments']['action'],action)
                def io(action,*extra):return relay.io_main(self.transport,[action,'--spool',str(new),'--number',str(number),*extra])
                io('claim');io('preflight')
                response={key:req[key] for key in ('protocol','session_id','request_id','operation')}
                response.update(type='response',ok=True,result={'events':[],'until':0,'done':True,'next':0})
                io('append','--text-json',json.dumps(json.dumps(response)+'\n'));io('publish')
        self.assertTrue(json.loads(p.stdout.readline())['ok'])
        self.assertEqual(p.wait(timeout=10),0);self.assertEqual(p.stderr.read(),'')
        replay=json.loads((new/'response-1.json').read_bytes());self.assertEqual(replay['result'],begin)
        self.assertNotEqual(replay['session_id'],requests[0]['session_id'])
        self.assertFalse((new/'invoke-1.json').exists());self.assertTrue((new/'replay-1.json').exists())
        receipt=json.loads((new/'replay-1.json').read_bytes())
        self.assertEqual(receipt['new_response_sha256'],hashlib.sha256((new/'response-1.json').read_bytes()).hexdigest())
        summary=json.loads((new/'replay-summary.json').read_bytes())
        self.assertEqual(summary['replayed_reads'],1)
        self.assertEqual(summary['replayed_response_bytes'],len((new/'response-1.json').read_bytes()))
        self.assertEqual(summary['cached_response_bytes'],len((old.path/'response-1.json').read_bytes()))
        self.assertEqual(json.loads((new/'result.json').read_bytes())['result']['transport_call_count'],3)
        self.assertEqual(d1_backup.verify_site_backup(self.root/'finished-backup')['manifest']['main_revision'],0)
        self.assertEqual(self.hashes(old.path),before)
        # Nested recovery is deliberately outside the first bounded implementation.
        with relay.existing(self.transport,new) as completed:
            with self.assertRaises(relay.RelayError):self.seal(completed,prefix=2)


if __name__=='__main__':unittest.main()
