import nunjucks from 'nunjucks/browser/nunjucks-slim.js';
import {templates,assets} from './generated-ui.js';
const env=new nunjucks.Environment(new nunjucks.PrecompiledLoader(templates),{autoescape:true});
env.addTest('none',v=>v==null);
env.addFilter('urlencode',v=>encodeURIComponent(v||''));
env.addFilter('authors',v=>{try{return JSON.parse(v||'[]').filter(Boolean).join('、')||'作者信息未提供';}catch{return '作者信息未提供';}});
env.addFilter('list_label',v=>{try{return JSON.parse(v||'[]').join('、');}catch{return '';}});
env.addFilter('publication_date',(v,p)=>!v?'日期未知':p==='month'?v.slice(0,7):p==='year'?v.slice(0,4):v);
env.addFilter('human_time',v=>{if(!v)return '尚无记录';const date=new Date(v);if(!Number.isFinite(date.getTime()))return v;return new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}).format(date);});
const statuses={active:'已启用',draft:'草稿',superseded:'已停用',succeeded:'成功',partial:'部分完成',failed:'失败',running:'运行中',exported:'待导入',imported:'已导入',abandoned:'已放弃',rejected:'已拒绝',healthy:'健康',degraded:'降级',unknown:'尚无记录',ok:'成功',eligible:'符合收录范围',excluded:'已排除',quarantine:'待核查',queued:'等待中',no_change:'无需调整',suggested:'已有建议',accepted:'已采用'};
env.addFilter('status_label',v=>statuses[v]||v||'尚无记录');
const checks={database_integrity:'数据库完整性',confirmed_profile:'已确认研究画像',source_coverage:'来源运行覆盖',source_runs:'最近来源运行',source_degradation:'来源降级',official_issue_coverage:'官网卷期覆盖',official_issue_failures:'官网核验失败',latest_semantic_job:'最近一次 Codex 判断',semantic_coverage:'相关性判断覆盖',abstract_coverage:'摘要覆盖',web_service:'后台网页服务',cloud_sync:'云端数据同步',recommendation_freshness:'推荐更新时效'};
env.addFilter('check_label',v=>checks[v]||v);
env.addGlobal('url_for',(_,kwargs)=>'/static'+kwargs.path);
export function staticAsset(path){const asset=assets[path];return asset?new Response(Uint8Array.from(atob(asset.data),c=>c.charCodeAt(0)),{headers:{'Content-Type':asset.type,'Cache-Control':'public, max-age=300','X-Content-Type-Options':'nosniff'}}):null;}
export function render(page,context){return env.render(page+'.html',context);}
