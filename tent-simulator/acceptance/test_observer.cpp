#include "acceptance_metrics.h"
#include <cassert>
#include <iostream>
int main() {
 acceptance::Changes c;c.observe(.5);c.observe(.5001);c.observe(.51);c.observe(.5101);c.observe(.49);
 assert(c.samples==5 && c.events[1]==2 && c.reversals[1]==1);
 acceptance::Collector x(250000000,750000000,true);
 x.decision(0,.1);x.decision(250000000,.2);x.decision(499999999,.4);x.decision(500000000,.3);x.decision(750000000,.9);
 assert(x.total.samples==3 && x.windows[0].samples==2 && x.windows[1].samples==1);
 assert(x.windows[1].events[1]==0 && x.total.reversals[1]==1);
 x.bytes(749999999,1,17,true);x.bytes(750000000,1,19,true);assert(x.done.back()[1]==17);
 acceptance::Collector off(0,1,false);off.decision(0,.5);assert(!off.total.samples);
 std::cout<<"OBSERVER_TESTS_OK\n";
}
