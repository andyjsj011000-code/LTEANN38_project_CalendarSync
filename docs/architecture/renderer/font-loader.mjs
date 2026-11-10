export async function load(url,context,nextLoad) {
 const result=await nextLoad(url,context);
 if(!url.replaceAll('\\','/').endsWith('/explainer/render.mjs'))return result;
 const text=Buffer.from(result.source).toString('utf8');
 const marker='export function renderExplainer(document, options = {}) {';
 if(!text.includes(marker))throw new Error('Readable rendering adapter: renderer signature changed');
 const adapter=new URL('./readable-render.mjs',import.meta.url).href;
 return {...result,source:text.replace(marker,'function renderExplainerOriginal(document, options = {}) {')+`\nimport { makeReadable } from ${JSON.stringify(adapter)};\nexport function renderExplainer(document,options={}) { return makeReadable(renderExplainerOriginal(document,options)); }\n`.replaceAll('\\n','\n')};
}
