"""Offline closed-timeout continuation tests. All callback data is synthetic."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import test_d1_native_spool as base
import test_d1_combined_read_journal as helpers
from test_d1_combined_read_journal import OLD_SESSION, NEW_SESSION
from academic_radar import d1_backup

relay = base.relay


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


@unittest.skipUnless(os.name == 'posix', 'Private relay requires POSIX')
class TimeoutReadJournalTests(unittest.TestCase):
    command = base.D1NativeSpoolTests.command
    stop = base.D1NativeSpoolTests.stop
    request = helpers.CombinedReadJournalTests.request
    response = helpers.CombinedReadJournalTests.response
    complete_transcript = helpers.CombinedReadJournalTests.complete_transcript
    publish_synthetic_response = helpers.CombinedReadJournalTests.publish_synthetic_response
    write_private = helpers.CombinedReadJournalTests.write_private
    change = helpers.CombinedReadJournalTests.change
    contents = helpers.CombinedReadJournalTests.contents
    original_source = helpers.CombinedReadJournalTests.original_source
    rejection = helpers.CombinedReadJournalTests.rejection

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Only the owned synthetic fixture root is canonicalized.
        self.root = Path(self.temp.name).resolve()
        self.manifest = self.root/'source.json'
        self.sha = relay.pin_source(self.manifest)['source_manifest_sha256']
        self.files = relay.verify_source(self.manifest,self.sha)
        self.transport = relay.make_transport(self.manifest,self.sha,self.files)

    def fixture(self, prefix=None, *, transcript=None, name='old-timeout'):
        if transcript is None:
            _,transcript,default,_ = self.complete_transcript()
            prefix = default if prefix is None else prefix
        old = self.transport.PrivateSpool(self.root,name)
        self.addCleanup(old.close)
        for number,(req,response) in enumerate(transcript[:prefix+1],1):
            raw = self.transport._encode(req);claim = {'request_sha256':sha(raw)}
            old.save('request-%d.json'%number,raw)
            old.save('pending-%d.json'%number,self.transport._encode({**claim,'heartbeat':'live-%d.json'%number,'deadline_unix_ms':1}))
            old.save('live-%d.json'%number,b'{}\n')
            if number <= prefix:
                old.save('claim-%d.json'%number,self.transport._encode(claim))
                old.save('invoke-%d.json'%number,self.transport._encode(claim))
                raw = self.transport._encode(response)
                old.save('response-%d.partial'%number,raw)
                old.save('response-%d.json'%number,raw)
        old.save('result.json',self.transport._encode({'protocol':self.transport.PROTOCOL,'type':'relay_error',
            'session_id':OLD_SESSION,'request_id':prefix+1,'ok':False,'error':{'code':'relay_timeout'}}))
        return {'old':old,'prefix':prefix,'transcript':transcript}

    def seal(self, fixture, name='journal.json', *, previous=None):
        path = self.root/name
        info = relay.seal_timeout_journal(self.transport,fixture['old'].path,fixture['prefix'],path,self.original_source,previous)
        return path,info

    def load(self, fixture, *, reserve=True):
        path,info = self.seal(fixture)
        journal = relay.TimeoutReadJournal(self.transport,path,info['journal_sha256'])
        if reserve: journal.reserve(self.root/'new-run')
        return journal,path,info

    def replay(self,journal,fixture):
        for request,response in fixture['transcript'][:fixture['prefix']]:
            actual = self.transport._decode(journal.response(dict(request,session_id=NEW_SESSION)))
            self.assertEqual(actual,dict(response,session_id=NEW_SESSION))

    def test_replays_successes_locally_and_only_exposes_exact_pending_boundary(self):
        fixture = self.fixture();before = self.contents(fixture['old'].path)
        with patch.object(relay.subprocess,'Popen',side_effect=AssertionError('No native spawn')):
            journal,_,info = self.load(fixture)
            self.replay(journal,fixture)
            boundary = dict(fixture['transcript'][fixture['prefix']][0],session_id=NEW_SESSION)
            self.assertIsNone(journal.response(boundary))
        self.assertEqual(info['next'],fixture['prefix']+1)
        self.assertEqual(info['boundary'],'never_invoked')
        self.assertEqual(journal.replayed,fixture['prefix'])
        self.assertEqual(self.contents(fixture['old'].path),before)

    def test_712_successful_reads_are_preserved_without_external_replay(self):
        helper = helpers.CombinedReadJournalTests();self.addCleanup(helper.doCleanups)
        helper.root = self.root/'synthetic-helper';helper.root.mkdir(mode=0o700)
        helper.transport,helper.sha = self.transport,self.sha
        sample = helper.fixture(prefix=712)
        fixture = self.fixture(712,transcript=sample['transcript'])
        journal,_,_ = self.load(fixture)
        self.replay(journal,fixture)
        self.assertEqual(journal.replayed,712)
        self.assertIsNone(journal.response(dict(fixture['transcript'][712][0],session_id=NEW_SESSION)))

    def test_any_tail_invocation_or_response_is_rejected(self):
        fixture = self.fixture();n = fixture['prefix']+1
        for kind in ('invoke','response','partial'):
            with self.subTest(kind=kind):
                path = fixture['old'].path/('response-%d.partial'%n if kind=='partial' else '%s-%d.json'%(kind,n))
                self.write_private(path,b'{}\n')
                with self.assertRaises(self.rejection): self.seal(fixture,kind+'.json')
                path.unlink()  # Only malformed synthetic evidence.

    def test_bound_claim_without_invocation_is_eligible(self):
        fixture = self.fixture();n=fixture['prefix']+1
        request_raw = (fixture['old'].path/('request-%d.json'%n)).read_bytes()
        fixture['old'].save('claim-%d.json'%n,self.transport._encode({'request_sha256':sha(request_raw)}))
        self.load(fixture)

    def test_timeout_before_announcement_is_eligible_but_orphan_live_is_not(self):
        fixture = self.fixture();n=fixture['prefix']+1
        (fixture['old'].path/('pending-%d.json'%n)).unlink()
        with self.assertRaises(self.rejection): self.seal(fixture,'bad.json')
        (fixture['old'].path/('live-%d.json'%n)).unlink()
        self.load(fixture)

    def test_wrong_terminal_code_or_type_is_not_reclassified(self):
        fixture=self.fixture();path=fixture['old'].path/'result.json';original=json.loads(path.read_bytes())
        for change in ({'type':'result'},{'error':{'code':'interrupted'}},{'error':{'code':'host_action_failed'}},
                       {'request_id':fixture['prefix']},{'ok':True},{'session_id':NEW_SESSION}):
            with self.subTest(change=change):
                self.change(path,{**original,**change})
                with self.assertRaises(self.rejection):self.seal(fixture,'bad.json')

    def test_failed_prefix_and_changed_partial_are_rejected(self):
        fixture=self.fixture();path=fixture['old'].path/'response-1.json';old=path.read_bytes()
        value=json.loads(old);value.pop('result');value.update(ok=False,error={'code':'native_rejected'})
        self.change(path,value)
        with self.assertRaises(self.rejection):self.seal(fixture)
        self.change(path,old);self.change(fixture['old'].path/'response-1.partial',b'{}\n')
        with self.assertRaises(self.rejection):self.seal(fixture)

    def test_expired_deadline_required_and_unknown_inventory_rejected(self):
        fixture=self.fixture();path=fixture['old'].path/('pending-%d.json'%(fixture['prefix']+1));old=path.read_bytes()
        value=json.loads(old);value['deadline_unix_ms']=9999999999999;self.change(path,value)
        with self.assertRaises(self.rejection):self.seal(fixture)
        self.change(path,old);fixture['old'].save('unreviewed.json',b'{}\n')
        with self.assertRaises(self.rejection):self.seal(fixture)

    def test_replay_order_session_body_and_fresh_fences_fail_closed(self):
        fixture=self.fixture();journal,_,_=self.load(fixture)
        first=dict(fixture['transcript'][0][0],session_id=NEW_SESSION)
        for bad in (dict(first,request_id=2),dict(first,session_id=OLD_SESSION),
                    dict(first,arguments={'action':'capacity','data':{}})):
            with self.assertRaises(self.rejection):journal.response(bad)
        self.replay(journal,fixture)
        tail=dict(fixture['transcript'][fixture['prefix']][0],session_id=NEW_SESSION)
        wrong=copy.deepcopy(tail);wrong['arguments']['data']['generation']='0'*64
        with self.assertRaises(self.rejection):journal.response(wrong)
        self.assertIsNone(journal.response(tail))
        number=tail['request_id']+1
        for operation,arguments in [('site.read_main_store',{'action':'head','data':{}}),
            ('site.read_site_backup',{'action':'begin','data':{}}),
            ('site.read_site_backup',{'action':'feedback','data':{'after':0,'until':999}}),
            ('site.read_site_backup',{'action':'records','data':{'generation':'0'*64}}),
            ('site.write_main_store',{'action':'commit_operations','data':{}})]:
            with self.assertRaises(self.rejection):journal.response(dict(tail,request_id=number,operation=operation,arguments=arguments))

    def test_existing_fence_blocks_copied_journal_or_spool(self):
        fixture=self.fixture();journal,path,info=self.load(fixture)
        copy_path=self.root/'copied-journal.json';self.write_private(copy_path,path.read_bytes())
        copied=relay.TimeoutReadJournal(self.transport,copy_path,info['journal_sha256'])
        with self.assertRaises(self.rejection):copied.reserve(self.root/'another-run')
        copied_spool=self.root/'copied-spool';shutil.copytree(fixture['old'].path,copied_spool)
        second=self.root/'resealed.json'
        seal=relay.seal_timeout_journal(self.transport,copied_spool,fixture['prefix'],second,self.original_source)
        another=relay.TimeoutReadJournal(self.transport,second,seal['journal_sha256'])
        self.assertEqual(another.value['resume_fence'],journal.value['resume_fence'])
        with self.assertRaises(self.rejection):another.reserve(self.root/'yet-another-run')

    def test_changed_evidence_after_sealing_is_rejected(self):
        fixture=self.fixture();journal,_,_=self.load(fixture,reserve=False)
        path=fixture['old'].path/'response-1.json';self.change(path,path.read_bytes()+b' ')
        with self.assertRaises(self.rejection):journal.reserve(self.root/'new-run')
        self.assertFalse(Path(journal.value['resume_fence']).exists())

    def test_symlink_hardlink_and_public_evidence_rejected(self):
        fixture=self.fixture();target=fixture['old'].path/'response-1.json';raw=target.read_bytes()
        os.chmod(target,0o644)
        with self.assertRaises(self.rejection):self.seal(fixture)
        os.chmod(target,0o400);extra=self.root/'hardlink';os.link(target,extra)
        with self.assertRaises(self.rejection):self.seal(fixture)
        extra.unlink();target.unlink();self.write_private(extra,raw);target.symlink_to(extra)
        with self.assertRaises(self.rejection):self.seal(fixture)

    def test_cli_seal_is_readonly_and_arbitrary_resume_commands_are_rejected(self):
        fixture=self.fixture();before=self.contents(fixture['old'].path);path=self.root/'cli-journal.json'
        result=subprocess.run(self.command('seal-timeout-journal','--spool',fixture['old'].path,'--prefix',fixture['prefix'],
            '--output',path,'--original-source-root',base.PROJECT,'--original-source-manifest',self.manifest,
            '--original-source-manifest-sha256',self.sha),capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(self.contents(fixture['old'].path),before)
        info=json.loads(result.stdout)
        result=subprocess.run(self.command('resume-timeout-backup','--workspace',self.root,'--spool','new-run',
            '--timeout-seconds',60,'--journal',path,'--journal-sha256',info['journal_sha256'],'--output',self.root/'out',
            '--','publish-activate'),capture_output=True,text=True,timeout=10)
        self.assertNotEqual(result.returncode,0)
        self.assertFalse((self.root/'new-run').exists())

    def test_pump_changes_are_rejected_despite_identical_application_source(self):
        fixture=self.fixture()
        checkout=base.D1NativeSpoolTests.checkout_copy(self)
        pump=checkout/'scripts/d1-native-pump.js'
        pump.write_bytes(pump.read_bytes()+b'\n// synthetic parity change\n')
        manifest=self.root/'changed-source.json'
        pin=relay.pin_source(manifest,checkout)['source_manifest_sha256']
        with self.assertRaises(self.rejection):
            relay.seal_timeout_journal(self.transport,fixture['old'].path,fixture['prefix'],self.root/'bad-journal.json',
                {'root':str(checkout),'manifest':str(manifest),'sha256':pin})

    def test_success_cannot_skip_local_replay_or_pending_boundary(self):
        fixture=self.fixture();journal,_,_=self.load(fixture)
        transport=relay.make_transport(self.manifest,self.sha,self.files,journal=journal)
        value={'protocol':transport.PROTOCOL,'type':'result','session_id':NEW_SESSION,
            'request_id':fixture['prefix'],'ok':True,'result':{}}
        with self.assertRaises(self.rejection):transport._curated_result(value,NEW_SESSION,fixture['prefix'])
        self.replay(journal,fixture)
        with self.assertRaises(self.rejection):transport._curated_result(value,NEW_SESSION,fixture['prefix'])
        journal.response(dict(fixture['transcript'][fixture['prefix']][0],session_id=NEW_SESSION))
        value['request_id']+=1
        self.assertEqual(transport._curated_result(value,NEW_SESSION,value['request_id']),value)

    def start(self,path,info,spool,output,seconds=60):
        process=subprocess.Popen(self.command('resume-timeout-backup','--workspace',self.root,'--spool',spool,
            '--timeout-seconds',seconds,'--journal',path,'--journal-sha256',info['journal_sha256'],'--output',output),
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        self.addCleanup(self.stop,process)
        return process

    def service(self,process,spool,expected,successful):
        line=process.stdout.readline();self.assertTrue(line,'Missing fresh request')
        event=json.loads(line);self.assertEqual(event['request_id'],expected['request_id'])
        self.assertEqual(event['operation'],'site.read_site_backup')
        request=json.loads((spool/('request-%d.json'%expected['request_id'])).read_bytes())
        self.assertEqual(request,dict(expected,session_id=request['session_id']))
        self.publish_synthetic_response(spool,request,successful['result'])
        return event

    def test_real_isolated_cli_completes_exact_backup_without_rereading_prefix(self):
        _,transcript,prefix,baseline=self.complete_transcript()
        fixture=self.fixture(prefix,transcript=transcript);before=self.contents(fixture['old'].path)
        path,info=self.seal(fixture);output=self.root/'resumed-output';process=self.start(path,info,'new-run',output)
        spool=self.root/'new-run'
        for req,response in transcript[prefix:]:self.service(process,spool,req,response)
        self.assertTrue(json.loads(process.stdout.readline())['ok']);self.assertEqual(process.wait(timeout=20),0)
        self.assertEqual(process.stderr.read(),'')
        self.assertEqual(d1_backup.verify_backup(output)['backup_sha256'],baseline['backup_sha256'])
        for number in range(1,prefix+1):
            self.assertTrue((spool/('replay-%d.json'%number)).is_file())
            self.assertFalse((spool/('invoke-%d.json'%number)).exists())
        self.assertEqual(self.contents(fixture['old'].path),before)

    def test_second_timeout_can_chain_only_with_verified_progress_and_previous_fence(self):
        _,transcript,prefix,baseline=self.complete_transcript()
        fixture=self.fixture(prefix,transcript=transcript);path,info=self.seal(fixture)
        process=self.start(path,info,'new-run',self.root/'unfinished',seconds=10)
        spool=self.root/'new-run'
        self.service(process,spool,*transcript[prefix])
        next_event=json.loads(process.stdout.readline());self.assertEqual(next_event['request_id'],prefix+2)
        self.assertEqual(process.wait(timeout=20),1)
        self.assertEqual(json.loads((spool/'result.json').read_bytes())['error'],{'code':'relay_timeout'})
        self.assertFalse((self.root/'unfinished').exists())
        previous={'path':str(path),'sha256':info['journal_sha256']}
        next_path=self.root/'next-journal.json'
        with self.assertRaises(self.rejection):
            relay.seal_timeout_journal(self.transport,spool,prefix+1,next_path,self.original_source)
        ancestor_before=self.contents(fixture['old'].path)
        with self.assertRaises(self.rejection):
            relay.seal_timeout_journal(self.transport,spool,prefix+1,fixture['old'].path/'forbidden.json',self.original_source,previous)
        self.assertEqual(self.contents(fixture['old'].path),ancestor_before)
        relocated=self.root/'relocated-control';relocated.mkdir(mode=0o700)
        copied_manifest=relocated/'source.json';self.write_private(copied_manifest,self.manifest.read_bytes())
        copied_root=base.D1NativeSpoolTests.checkout_copy(self)
        for changed in (dict(self.original_source,manifest=str(copied_manifest)),dict(self.original_source,root=str(copied_root))):
            with self.assertRaises(self.rejection):
                relay.seal_timeout_journal(self.transport,spool,prefix+1,next_path,changed,previous)
        self.assertFalse(next_path.exists())
        next_info=relay.seal_timeout_journal(self.transport,spool,prefix+1,next_path,self.original_source,previous)
        candidate=relay.TimeoutReadJournal(self.transport,next_path,next_info['journal_sha256'])
        with patch.object(candidate,'reserved',True):
            with self.assertRaises(self.rejection):candidate.response(transcript[0][0])
        with self.assertRaises(self.rejection):candidate.validate_destinations(fixture['old'].path/'forbidden-output')
        child=self.start(next_path,next_info,'last-run',self.root/'finished')
        for req,response in transcript[prefix+1:]:self.service(child,self.root/'last-run',req,response)
        self.assertTrue(json.loads(child.stdout.readline())['ok']);self.assertEqual(child.wait(timeout=20),0)
        self.assertEqual(child.stderr.read(),'')
        self.assertEqual(d1_backup.verify_backup(self.root/'finished')['backup_sha256'],baseline['backup_sha256'])
        self.assertTrue(Path(relay.TimeoutReadJournal(self.transport,path,info['journal_sha256']).value['resume_fence']).exists())


if __name__=='__main__':unittest.main()
