#pragma once
#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0601
#endif
#include <windows.h>
#include <shobjidl.h>

// One-shot, same-COM-thread diagnostic proxy. The caller owns the initial ref.
// The proxy AddRefs borrowed_real; it never calls borrowed_real->Show/Close.
// nullptr fixture_path is cancel-only: no shell items and no OnFileOk callbacks.
// Otherwise fixture_path must name an existing trusted local absolute file.
// Nonnull fixture mode remains diagnostic only. SetFileTypes retains owned
// copies and supports '*'/'*.*' or semicolon-separated '*.ext' patterns with
// ASCII letters/digits/dot/_/-/+ in the suffix, with surrounding spaces/tabs.
// Empty/null/malformed filters fail; other wildcard grammars are unsupported.
// FOS_STRICTFILETYPES opts into a conservative local Open-proxy policy (the
// Windows documentation describes that flag for Save): validate against the
// real dialog's current 1-based filter index, using case-insensitive suffixes.
// A strict mismatch/missing/out-of-range filter fails with E_INVALIDARG.
// Non-strict supported filters do not constrain the fixture extension.
// Show rejects FOS_PICKFOLDERS and unreadable options, and succeeds only if at least
// one OnFileOk was called and GetResults succeeded during an OnFileOk callback.
// A missing/noop sink fails with E_UNEXPECTED; GetSelectedItems/GetResult alone
// do not satisfy that Qt-specific acceptance contract. Cancel-only is exempt.
// Show has a five-second creation lease, checked before/after synchronous COM
// callbacks; this is cooperative and cannot preempt a blocked external sink.
// S_FALSE veto ends this headless attempt as ERROR_CANCELLED (there is no UI
// in which to revise the selection). Close with a success code is normalized
// to ERROR_CANCELLED, so a closed attempt never reports S_OK without results.
// Advise is local to the proxy. Unadvise and Release remain valid after expiry.
// All interface calls except IUnknown ref/identity operations require the
// creating thread. Final Release must occur on that same COM thread.
constexpr ULONGLONG WXBG_FILE_PROXY_LEASE_MS = 5000;
HRESULT CreateWxBgFileOpenProxy(IFileOpenDialog *borrowed_real,
                               const wchar_t *fixture_path,
                               IFileOpenDialog **out);
using WxBgFileProxyAuthorize = BOOL (WINAPI *)(void *context, ULONGLONG generation);
struct WxBgFileProxyGrant {
    ULONGLONG generation;
    ULONGLONG deadline_tick; // Absolute GetTickCount64 deadline; expiry cancels.
    WxBgFileProxyAuthorize authorize;
    void *context;
};
// Positive live routing must use this factory. The record is copied by value;
// callback/context must remain valid until final Release (resident module data,
// never borrowed stack state). authorize must be bounded and nonblocking, and
// check the copied generation against current revocation state. Cancel-only
// ignores the grant. A nonnull fixture requires a nonnull, well-formed grant.
HRESULT CreateWxBgFileOpenProxyWithGrant(IFileOpenDialog *borrowed_real,
                                        const wchar_t *fixture_path,
                                        const WxBgFileProxyGrant *grant,
                                        IFileOpenDialog **out);
// Diagnostic only: live proxy objects, including a synchronous Show in flight.
// Zero is not a general proof that no caller is entering this module's code.
extern "C" __declspec(dllexport) LONG WxBgFileOpenProxyLiveCount() noexcept;
// Cumulative completed null-fixture Show cancellations. Rejected cross-thread
// calls and repeat calls on the same one-shot proxy do not increment it.
extern "C" __declspec(dllexport) LONG WxBgFileOpenProxyCancelledShowCount() noexcept;
extern "C" __declspec(dllexport) LONG WxBgFileOpenProxyAcceptedShowCount() noexcept;
// Successful GetResults calls made inside OnFileOk, including later-vetoed
// attempts. This diagnostic alone must never be interpreted as acceptance.
extern "C" __declspec(dllexport) LONG WxBgFileOpenProxyCallbackGetResultsCount() noexcept;
