import assert from 'node:assert/strict';
import {render} from '../worker/render.js';

const common={page:'profile',site_mode:'cloud',versions:[],profile_review:{},
  active:{content:'Legacy confirmed interests',version_number:1},cloud_sync_status:{}};
const profile={version:4,generation:'a'.repeat(64),content:'Owner interests <exact>',
  screening_preferences:{relevance_threshold:.85,instructions:'Use concrete mechanism evidence.'},pending:true};
const current=render('profile',{...common,cloud_profile:profile,profile_editable:true});
assert.ok(current.includes('action="/profile/save"'));
assert.ok(current.includes('name="base_version" value="4"'));
assert.ok(current.includes('name="generation" value="'+profile.generation+'"'));
assert.ok(current.includes('Owner interests &lt;exact&gt;'));

assert.ok(!current.includes('name="relevance_threshold"'));assert.ok(!current.includes('name="instructions"'));assert.ok(current.includes('name="profile_format" value="single"'));

assert.ok(current.includes('等待本地同步'));
assert.ok(!current.includes('action="/profile/draft"'));
const retry=render('profile',{...common,cloud_profile:profile,profile_editable:true,
  profile_error:'Synthetic version conflict',profile_submission:{...profile,
    base_version:3,content:'Unsaved owner input',summary:'Keep my input'}});
assert.ok(retry.includes('name="base_version" value="3"'));
assert.ok(retry.includes('Unsaved owner input'));
assert.ok(retry.includes('Synthetic version conflict'));
const legacy=render('profile',common);
assert.ok(legacy.includes('Legacy confirmed interests'));
assert.ok(legacy.includes('readonly'));
assert.ok(!legacy.includes('action="/profile/save"'));
assert.ok(!legacy.includes('action="/profile/draft"'));
assert.ok(!legacy.includes('signin-with-chatgpt'));
const local=render('profile',{...common,site_mode:'local'});
assert.ok(local.includes('action="/profile/draft"'));
assert.ok(!local.includes('action="/profile/save"'));
console.log('Profile UI preserves v10 version/generation/preferences, conflict input, legacy reads and local confirmation.');
