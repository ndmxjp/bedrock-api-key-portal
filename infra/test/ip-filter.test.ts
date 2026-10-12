import { describe, expect, it } from "vitest";
import { ipFilterFunctionCode, parseIpv4Cidr } from "../lib/ip-filter.js";

function load(cidrs: string[]) {
  // Evaluate the generated CloudFront Function source in isolation.
  return new Function(`${ipFilterFunctionCode(cidrs)}; return handler;`)() as (e: unknown) => any;
}

const ev = (ip: string, uri = "/") => ({ viewer: { ip }, request: { uri, headers: {} } });

describe("IP filter CloudFront Function", () => {
  const handler = load(["203.0.113.0/24", "198.51.100.7/32"]);

  it("passes listed addresses through unchanged", () => {
    const e = ev("203.0.113.42");
    expect(handler(e)).toBe(e.request);
    expect(handler(ev("198.51.100.7"))).toEqual({ uri: "/", headers: {} });
  });

  it("blocks everything else with 403 (JSON for the API, HTML for the site)", () => {
    const api = handler(ev("203.0.114.1", "/api/me"));
    expect(api.statusCode).toBe(403);
    expect(JSON.parse(api.body).error.code).toBe("IP_NOT_ALLOWED");
    const site = handler(ev("198.51.100.8"));
    expect(site.statusCode).toBe(403);
    expect(site.headers["content-type"].value).toContain("text/html");
  });

  it("rejects malformed and IPv6 viewer addresses", () => {
    for (const ip of ["2001:db8::1", "1.2.3", "999.1.1.1", "", "a.b.c.d"]) {
      expect(handler(ev(ip)).statusCode).toBe(403);
    }
  });

  it("handles /0 and high addresses (unsigned math)", () => {
    expect(load(["0.0.0.0/0"])(ev("255.255.255.255")).statusCode).toBeUndefined();
    expect(load(["255.255.255.0/24"])(ev("255.255.255.9")).statusCode).toBeUndefined();
    expect(load(["255.255.255.0/24"])(ev("254.255.255.9")).statusCode).toBe(403);
  });

  it("validates CIDRs", () => {
    expect(parseIpv4Cidr("10.0.0.5/8")).toEqual([0x0a000000, 0xff000000]);
    for (const bad of ["10.0.0.0", "10.0.0.0/33", "300.0.0.0/8", "2001:db8::/32"]) {
      expect(() => parseIpv4Cidr(bad)).toThrow();
    }
  });

  it("stays well under the 10 KB function size limit", () => {
    const many = Array.from({ length: 100 }, (_, i) => `10.${i}.0.0/16`);
    expect(ipFilterFunctionCode(many).length).toBeLessThan(10 * 1024);
  });
});
