"""Independent exhaustive integer replay of the TG controller; SSH only."""
import math


class GainReplay:
    def __init__(self, capacities, gain):
        self.c0, self.c1 = capacities
        self.base = self.held = math.floor(self.c0 / (self.c0 + self.c1) * 16 + .5)
        self.gain = gain
        self.last = self.window = self.samples = self.changed_at = self.confirmations = 0
        self.sum0 = self.sum1 = 0.
        self.proposed = -1
        self.events = []

    def next(self, now, q0, q1):
        if self.gain:
            if not self.last or now - self.last > 200000000:
                self.window = now; self.sum0 = self.sum1 = 0.; self.samples = 0
                self.confirmations = 0; self.proposed = -1
            self.last = now
            self.sum0 += q0; self.sum1 += q1; self.samples += 1
            if now - self.window >= 20000000:
                a, b = self.sum0/self.samples, self.sum1/self.samples

                def finish(k):
                    return max((a+k*65536)/self.c0 if k else 0.,
                               (b+(16-k)*65536)/self.c1 if k<16 else 0.)

                # Enumerate all 17 integer choices; do not use the C++ closed form.
                best = min(range(17),key=lambda k:(finish(k),abs(k-self.held),k))
                target = self.held + max(-2,min(2,best-self.held))
                improvement = finish(self.held)-finish(target)
                margin = max(32768/min(self.c0,self.c1),104857.6/(self.c0+self.c1))
                previous = self.held
                if target!=self.held and improvement>margin:
                    self.confirmations = min(3,self.confirmations+1) if target==self.proposed else 1
                    self.proposed = target
                    if self.confirmations==3 and (not self.changed_at or now-self.changed_at>=100000000):
                        self.held = target; self.changed_at = now
                        self.confirmations = 0; self.proposed = -1
                else:
                    self.confirmations = 0; self.proposed = -1
                self.events.append(dict(ns=now,previous=previous,held=self.held,best=best,target=target,
                    actual_step_gain_seconds=improvement,margin_seconds=margin,changed=previous!=self.held))
                self.window = now; self.sum0 = self.sum1 = 0.; self.samples = 0
        return self.held,self.held-self.base
