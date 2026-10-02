// 详情页密钥打码 —— 直接跑真模块
import { looksLikeSecret, maskSecret, maskSecretsDeep } from '../../web/src/lib/maskSecret';

const fail = (m: string) => { console.error('FAIL', m); process.exit(1); };
const KEY = 'pfk_FAKEtestKEYforFixtureONLY0000';
if (maskSecret('xpftkey', KEY) !== 'pfk_••••••0000') fail('xpftkey 应打码: ' + maskSecret('xpftkey', KEY));
if (maskSecret('X-Anything', KEY) === KEY) fail('pfk_ 前缀不论字段名都要打码');
if (maskSecret('xpftkey', '{{data_platform_key}}') !== '{{data_platform_key}}') fail('占位不打码');
if (maskSecret('Authorization', 'Bearer {{credential}}') !== 'Bearer {{credential}}') fail('凭证占位不打码');
if (maskSecret('Content-Type', 'application/json') !== 'application/json') fail('普通头不打码');
if (maskSecret('token_url', 'https://example.com/oauth/token') !== 'https://example.com/oauth/token') fail('URL 不打码');
if (!looksLikeSecret('xpftkey', KEY) || looksLikeSecret('xpftkey', '{{data_platform_key}}')) fail('looksLikeSecret');
const deep = maskSecretsDeep({ url: 'https://x/y', headers: { xpftkey: KEY, 'Content-Type': 'application/json' }, n: 3 });
if (JSON.stringify(deep).includes(KEY)) fail('type_config 原文里也要打码');
if (deep.headers['Content-Type'] !== 'application/json' || deep.n !== 3 || deep.url !== 'https://x/y') fail('其它字段原样');
console.log('OK');
