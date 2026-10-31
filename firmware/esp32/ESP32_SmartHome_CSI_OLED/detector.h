#pragma once
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>
#include "config.h"

// Classic ESP32, LLTF-only, 20 MHz; bins +6..+26 and -26..-6.
// Never reads first four bytes (first_word_invalid), DC or guard bins.
constexpr int TONES = 42;
struct Frame {
    uint32_t ms;
    uint32_t generation;
    int8_t raw[128];
    uint8_t mode, channel;
};
struct Result {
    enum State { SETTLING, CALIBRATING, READY, NO_DATA } state = NO_DATA;
    float score = 0, on = 0, off = 0;
    bool motion = false;
    unsigned samples = 0, calibrated = 0;
    uint32_t ms = 0;
    uint32_t generation = 0;
};

class Detector {
    float sum[TONES]{}, squares[TONES]{};
    float baseline[CSI_CALIBRATION_WINDOWS]{};
    unsigned n = 0, calibration = 0;
    unsigned high = 0, low = 0;
    uint32_t start = 0, last = 0, since = 0;
    bool haveFrame = false;
    uint8_t mode = 255, channel = 0;
    Result output;

    void clearWindow() {
        memset(sum, 0, sizeof(sum));
        memset(squares, 0, sizeof(squares));
        n = 0;
    }
    static float median(float *values, unsigned count) {
        std::sort(values, values + count);
        return count % 2 ? values[count / 2] :
            (values[count / 2 - 1] + values[count / 2]) / 2;
    }
public:
    void reset(uint32_t now) {
        clearWindow(); calibration = high = low = 0;
        since = now; haveFrame = false; mode = 255;
        output = Result{};
    }
    Result status() const { return output; }
    void noData(uint32_t now) {
        clearWindow(); high = low = 0; haveFrame = false;
        output.state = Result::NO_DATA; output.motion = false;
        output.samples = 0; output.ms = now;
        // Incomplete calibration must use contiguous valid windows.
        if (calibration < CSI_CALIBRATION_WINDOWS) calibration = 0;
        output.calibrated = calibration;
    }
    bool add(const Frame &f) {
        if (mode != f.mode || channel != f.channel) {
            reset(f.ms); mode = f.mode; channel = f.channel;
        }
        if (haveFrame && uint32_t(f.ms - last) > CSI_MAX_GAP_MS)
            noData(f.ms);
        last = f.ms; haveFrame = true;
        if (uint32_t(f.ms - since) < CSI_SETTLE_MS) {
            output.state = Result::SETTLING; output.ms = f.ms;
            return false;
        }
        float a[TONES]; float scale = 0;
        for (int i = 0; i < TONES; ++i) {
            const int bin = i < 21 ? i + 6 : i - 21 + 38;
            const float im = f.raw[2 * bin], re = f.raw[2 * bin + 1];
            a[i] = std::sqrt(im * im + re * re); scale += a[i];
        }
        scale /= TONES;
        if (scale < 1.0f) { noData(f.ms); return false; }
        if (n == 0) start = f.ms;
        for (int i = 0; i < TONES; ++i) {
            // Suppress common gain changes; also removes some real motion.
            const float x = a[i] / scale;
            sum[i] += x; squares[i] += x * x;
        }
        ++n;
        if (uint32_t(f.ms - start) < CSI_WINDOW_MS) return false;
        if (n < CSI_MIN_PACKETS) { noData(f.ms); return true; }
        float cv[TONES]; unsigned good = 0;
        for (int i = 0; i < TONES; ++i) {
            const float mean = sum[i] / n;
            if (mean > 0.1f)
                cv[good++] = std::sqrt((std::max)(0.0f,
                    squares[i] / n - mean * mean)) / mean;
        }
        if (good < TONES / 2) { noData(f.ms); return true; }
        output.score = median(cv, good);
        output.samples = n; output.ms = f.ms; clearWindow();
        if (calibration < CSI_CALIBRATION_WINDOWS) {
            baseline[calibration++] = output.score;
            output.state = Result::CALIBRATING;
            output.calibrated = calibration;
            if (calibration == CSI_CALIBRATION_WINDOWS) {
                float temp[CSI_CALIBRATION_WINDOWS];
                std::copy(baseline, baseline + calibration, temp);
                float center = median(temp, calibration);
                for (unsigned i = 0; i < calibration; ++i)
                    temp[i] = std::fabs(baseline[i] - center);
                float margin = (std::max)(CSI_MIN_MARGIN,
                    CSI_MAD_MULTIPLIER * median(temp, calibration));
                output.on = center + margin;
                output.off = center + margin * 0.5f;
                output.state = Result::READY;
            }
        } else {
            output.state = Result::READY;
            high = output.score > output.on ? high + 1 : 0;
            low = output.score < output.off ? low + 1 : 0;
            if (high >= CSI_ON_WINDOWS) output.motion = true;
            if (low >= CSI_OFF_WINDOWS) output.motion = false;
        }
        return true;
    }
};
