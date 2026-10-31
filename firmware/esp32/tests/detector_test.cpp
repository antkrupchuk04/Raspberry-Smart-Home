#include <cassert>
#include <cstdio>
#include "detector.h"

static Frame packet(uint32_t ms, bool motion = false) {
    Frame f{}; f.ms = ms; f.mode = 1; f.channel = 6;
    for (int bin = 0; bin < 64; ++bin) {
        f.raw[2 * bin] = 0;
        // Changes across frequency, not just common amplitude scaling.
        f.raw[2 * bin + 1] = motion ?
            ((bin + ms / 20) % 2 ? 35 : 65) : 50;
    }
    return f;
}
int main() {
    Detector d; d.reset(0);
    uint32_t ms = 0;
    for (; ms < 44000; ms += 20) d.add(packet(ms));
    assert(d.status().state == Result::READY);
    assert(d.status().calibrated == CSI_CALIBRATION_WINDOWS);
    assert(!d.status().motion);
    assert(d.status().score < 0.001f);
    for (; ms < 49000; ms += 20) d.add(packet(ms, true));
    assert(d.status().motion);
    for (; ms < 55000; ms += 20) d.add(packet(ms));
    assert(!d.status().motion);
    d.noData(ms + 1000);
    assert(d.status().state == Result::NO_DATA);
    assert(d.status().samples == 0);
    // Valid threshold survives a short outage, but not a channel change.
    assert(d.status().calibrated == CSI_CALIBRATION_WINDOWS);
    Frame changed = packet(ms + 1020); changed.channel = 11;
    d.add(changed);
    assert(d.status().state == Result::SETTLING);
    assert(d.status().calibrated == 0);
    // Corrupt first word cannot affect selected bins.
    Detector a, b; a.reset(0); b.reset(0);
    for (ms = 0; ms < 44000; ms += 20) {
        Frame f = packet(ms); a.add(f);
        f.raw[0] = -128; f.raw[1] = 127;
        f.raw[2] = -128; f.raw[3] = 127; b.add(f);
    }
    assert(a.status().score == b.status().score);
    // Sparse packets must never accumulate a false calibration.
    Detector sparse; sparse.reset(0);
    for (ms = 0; ms < 100000; ms += 300) sparse.add(packet(ms));
    assert(sparse.status().state != Result::READY);
    puts("PASS: calibration, motion, clearing, no-data, channel change, invalid bytes, sparse traffic");
}
