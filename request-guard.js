'use strict';

(function expose(factory) {
  const guards = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = guards;
  if (typeof globalThis !== 'undefined') Object.assign(globalThis, guards);
})(function createRequestGuard() {
  class RequestGuard {
    constructor() { this.generation = 0; this.controller = new AbortController(); this.sequences = {}; }
    reset() {
      this.controller.abort('lifecycle ended');
      this.controller = new AbortController();
      this.generation += 1;
      this.sequences = {};
    }
    begin(resource) {
      const sequence = resource ? (this.sequences[resource] || 0) + 1 : 0;
      if (resource) this.sequences[resource] = sequence;
      return { generation: this.generation, resource, sequence, signal: this.controller.signal };
    }
    isCurrent(ticket) {
      return ticket.generation === this.generation && !ticket.signal.aborted
        && (!ticket.resource || this.sequences[ticket.resource] === ticket.sequence);
    }
  }
  class ExclusiveActionGuard {
    constructor() { this.owner = null; this.sequence = 0; }
    get pending() { return this.owner !== null; }
    acquire() { if (this.pending) return null; this.owner = ++this.sequence; return this.owner; }
    release(owner) { if (owner === this.owner) this.owner = null; }
    reset() { this.owner = null; }
  }
  return { RequestGuard, ExclusiveActionGuard };
});
