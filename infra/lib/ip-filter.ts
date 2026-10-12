/**
 * CloudFront Function (cloudfront-js-2.0) source that only lets listed IPv4 CIDRs through.
 * Used for both the SPA and /api/* behaviors. IPv6 is disabled on the distribution when this
 * is enabled, so viewers always arrive over IPv4.
 */

const IPV4_CIDR = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\/(\d{1,2})$/;

export function parseIpv4Cidr(cidr: string): [number, number] {
  const m = IPV4_CIDR.exec(cidr.trim());
  if (!m) throw new Error(`invalid IPv4 CIDR "${cidr}"`);
  const octets = m.slice(1, 5).map(Number);
  const bits = Number(m[5]);
  if (octets.some((o) => o > 255) || bits > 32) throw new Error(`invalid IPv4 CIDR "${cidr}"`);
  const addr = ((octets[0] << 24) | (octets[1] << 16) | (octets[2] << 8) | octets[3]) >>> 0;
  const mask = bits === 0 ? 0 : (0xffffffff << (32 - bits)) >>> 0;
  return [(addr & mask) >>> 0, mask];
}

export function ipFilterFunctionCode(cidrs: string[]): string {
  const ranges = cidrs.map(parseIpv4Cidr);
  // Keep the function tiny and dependency-free (CloudFront Functions limits: 10 KB, 1 ms).
  return `var RANGES = ${JSON.stringify(ranges)};
function toInt(ip) {
  var p = ip.split(".");
  if (p.length !== 4) return -1;
  var n = 0;
  for (var i = 0; i < 4; i++) {
    var o = parseInt(p[i], 10);
    if (isNaN(o) || o < 0 || o > 255) return -1;
    n = n * 256 + o;
  }
  return n;
}
function allowed(ip) {
  var n = toInt(ip);
  if (n < 0) return false;
  for (var i = 0; i < RANGES.length; i++) {
    if (((n & RANGES[i][1]) >>> 0) === RANGES[i][0]) return true;
  }
  return false;
}
function handler(event) {
  var request = event.request;
  if (allowed(event.viewer.ip)) return request;
  var api = request.uri.indexOf("/api/") === 0;
  return {
    statusCode: 403,
    statusDescription: "Forbidden",
    headers: {
      "content-type": { value: api ? "application/json; charset=utf-8" : "text/html; charset=utf-8" },
      "cache-control": { value: "no-store" }
    },
    body: api
      ? '{"error":{"code":"IP_NOT_ALLOWED","message":"\\u3053\\u306e\\u30cd\\u30c3\\u30c8\\u30ef\\u30fc\\u30af\\u304b\\u3089\\u306f\\u5229\\u7528\\u3067\\u304d\\u307e\\u305b\\u3093\\u3002"}}'
      : '<!doctype html><html lang="ja"><meta charset="utf-8"><title>403</title><p>\\u3053\\u306e\\u30cd\\u30c3\\u30c8\\u30ef\\u30fc\\u30af\\u304b\\u3089\\u306f\\u5229\\u7528\\u3067\\u304d\\u307e\\u305b\\u3093\\u3002</p></html>'
  };
}
`;
}
