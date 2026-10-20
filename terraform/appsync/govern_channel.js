// AppSync Events — "govern" channel namespace handler (APPSYNC_JS runtime).
//
// Subscribers authenticate with their Cognito ID token (the namespace's only
// subscribe auth mode). A signed-in user may subscribe ONLY to their own
// tenant's channel, /govern/<encoded tenant>. The tenant is derived exactly as
// the API derives it (lambdas/shared/auth.py): the verified custom:tenantId
// claim, else the private "u-<sub>" workspace; a malformed claim is refused.
// Wildcards ("/govern/*") never match an encoded tenant, so they are refused.
//
// Publishing is IAM-only (the namespace's publish auth mode): only the activity
// stream Lambda can publish. The encoding must stay in step with
// lambdas/shared/govern/realtime.py (channel_for) and the web app's
// lib/govern/realtime-logic.ts.
import { util } from '@aws-appsync/utils';

const NAMESPACE = 'govern';
const SEGMENT_MAX = 50;
const ALNUM = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';

// APPSYNC_JS has no classic for/while loops and no ++: iterate with for-of.
function onlyAllowed(s, extra) {
  let ok = true;
  for (const ch of s.split('')) {
    if (ALNUM.indexOf(ch) < 0 && extra.indexOf(ch) < 0) ok = false;
  }
  return ok;
}

function isTenant(t) {
  if (typeof t !== 'string' || t.length < 1 || t.length > 64) return false;
  return ALNUM.indexOf(t.charAt(0)) >= 0 && onlyAllowed(t, '_.-');
}

function isSub(s) {
  return typeof s === 'string' && s.length >= 1 && s.length <= 60 && onlyAllowed(s, '-');
}

function tenantOf(claims) {
  const raw = claims['custom:tenantId'];
  if (typeof raw === 'string' && raw.trim().length > 0) {
    const tenant = raw.trim();
    return isTenant(tenant) ? tenant : null;
  }
  return isSub(claims.sub) ? 'u-' + claims.sub : null;
}

function channelFor(tenant) {
  const encoded = tenant.split('-').join('-0').split('_').join('-1').split('.').join('-2');
  // A valid tenant encodes to at most 128 characters: at most three segments.
  let path = '/' + NAMESPACE;
  for (const start of [0, SEGMENT_MAX, 2 * SEGMENT_MAX]) {
    if (start < encoded.length) path = path + '/' + encoded.substring(start, start + SEGMENT_MAX);
  }
  return path;
}

export function onSubscribe(ctx) {
  const claims = (ctx.identity && ctx.identity.claims) || {};
  const tenant = tenantOf(claims);
  const raw = (ctx.info && ctx.info.channel && ctx.info.channel.path) || '';
  const path = raw.charAt(0) === '/' ? raw : '/' + raw;
  if (!tenant || path !== channelFor(tenant)) {
    util.unauthorized();
  }
}
