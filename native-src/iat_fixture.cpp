#include <windows.h>

extern "C" {
__declspec(dllexport) int IatFixtureOriginal() { return 111; }
__declspec(dllexport) int IatFixtureReplacement() { return 222; }
// A dedicated image page lets tests change protection without touching CRT data.
__declspec(dllexport) __attribute__((section(".iatfx"), aligned(4096)))
void* volatile IatFixtureSlot = reinterpret_cast<void*>(&IatFixtureOriginal);
}
