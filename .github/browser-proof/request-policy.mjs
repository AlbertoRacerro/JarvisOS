const SAFE_BROWSER_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);

export function isMutatingSameOriginRequest(method, path, readOnlySameOriginPostPaths = new Set()) {
  if (typeof method !== 'string' || typeof path !== 'string' || !(readOnlySameOriginPostPaths instanceof Set)) return true;
  const normalizedMethod = method.toUpperCase();
  if (SAFE_BROWSER_METHODS.has(normalizedMethod)) return false;
  if (path.includes('%')) return true;
  if (normalizedMethod === 'POST' && readOnlySameOriginPostPaths.has(path)) return false;
  return true;
}
