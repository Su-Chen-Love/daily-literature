import {readFile,writeFile,readdir,mkdir} from 'node:fs/promises';
import nunjucks from 'nunjucks';
import {build} from 'esbuild';
const templates=[];
for(const name of (await readdir('shared/templates')).sort()) {
 const source=await readFile('shared/templates/'+name,'utf8');
 templates.push(nunjucks.precompileString(source,{name,wrapper:items=>items.map(t=>JSON.stringify(t.name)+':(function(){'+t.template+'})()').join(',')}));
}
const assets={};
for(const [path,type] of [['app.css','text/css; charset=utf-8'],['app.js','text/javascript; charset=utf-8'],['images/academic-radar-logo.png','image/png'],['images/daily-literature-logo.svg','image/svg+xml']]) {
 const data=await readFile('shared/static/'+path);
 assets['/static/'+path]={type,data:data.toString('base64')};
}
await writeFile('worker/generated-ui.js','export const templates={'+templates.join(',')+'};\nexport const assets='+JSON.stringify(assets)+';\n');
await mkdir('dist/server',{recursive:true});
await build({entryPoints:['worker/index.js'],outfile:'dist/server/index.js',bundle:true,format:'esm',platform:'browser',target:'es2022',legalComments:'none',plugins:[{name:'precompiled-templates-only',setup(b){b.onLoad({filter:/nunjucks-slim\.js$/},async({path})=>{const code=await readFile(path,'utf8'),needle='var func = new Function(source);';if(!code.includes(needle))throw new Error('Nunjucks runtime changed; review compile guard.');return {contents:code.replace(needle,"throw new Error('Only precompiled templates are supported.'); var func;"),loader:'js'};});}}]});
