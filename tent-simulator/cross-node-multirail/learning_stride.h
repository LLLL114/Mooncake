#pragma once

extern "C" int tent_e_set_stride(unsigned value) noexcept;
extern "C" int tent_e_error() noexcept;
bool tent_e_should_learn(int device) noexcept;
