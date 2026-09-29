#include "file_dialog_proxy.h"
#include <shlobj.h>
#include <algorithm>
#include <new>
#include <string>
#include <vector>

static volatile LONG live_objects = 0;
static volatile LONG cancelled_shows = 0;
static volatile LONG accepted_shows = 0;
static volatile LONG callback_result_reads = 0;
extern "C" __declspec(dllexport) LONG WxBgFileOpenProxyLiveCount() noexcept {
    return InterlockedCompareExchange(&live_objects, 0, 0);
}
extern "C" __declspec(dllexport) LONG WxBgFileOpenProxyCancelledShowCount() noexcept {
    return InterlockedCompareExchange(&cancelled_shows, 0, 0);
}
extern "C" __declspec(dllexport) LONG WxBgFileOpenProxyAcceptedShowCount() noexcept {
    return InterlockedCompareExchange(&accepted_shows, 0, 0);
}
extern "C" __declspec(dllexport) LONG WxBgFileOpenProxyCallbackGetResultsCount() noexcept {
    return InterlockedCompareExchange(&callback_result_reads, 0, 0);
}

namespace {
constexpr HRESULT cancelled = HRESULT_FROM_WIN32(ERROR_CANCELLED);
constexpr HRESULT timed_out = HRESULT_FROM_WIN32(ERROR_TIMEOUT);
constexpr HRESULT unsupported = HRESULT_FROM_WIN32(ERROR_NOT_SUPPORTED);

struct OwnedFilter {
    std::wstring name, pattern;
    std::vector<std::wstring> suffixes;
    bool match_all = false;
};

HRESULT CopyFilterText(const wchar_t *text, std::wstring &out) {
    if (!text) return E_INVALIDARG;
    size_t length = 0;
    while (length < 4096 && text[length]) ++length;
    if (!length || length == 4096) return E_INVALIDARG;
    out.assign(text, length);
    return S_OK;
}

HRESULT ParseFilter(OwnedFilter &filter) {
    size_t start = 0;
    while (start <= filter.pattern.size()) {
        const size_t separator = filter.pattern.find(L';', start);
        const size_t stop = separator == std::wstring::npos ? filter.pattern.size() : separator;
        const std::wstring raw = filter.pattern.substr(start, stop - start);
        const size_t first = raw.find_first_not_of(L" \t");
        if (first == std::wstring::npos) return E_INVALIDARG;
        const size_t last = raw.find_last_not_of(L" \t");
        const std::wstring token = raw.substr(first, last - first + 1);
        if (token == L"*" || token == L"*.*") filter.match_all = true;
        else {
            if (token.size() < 3 || token[0] != L'*' || token[1] != L'.') return unsupported;
            if (token.back() == L'.' || token.find(L"..") != std::wstring::npos) return E_INVALIDARG;
            for (size_t i = 2; i < token.size(); ++i) {
                const wchar_t c = token[i];
                if (!((c >= L'a' && c <= L'z') || (c >= L'A' && c <= L'Z') ||
                      (c >= L'0' && c <= L'9') || c == L'.' || c == L'_' || c == L'-' || c == L'+')) return unsupported;
            }
            filter.suffixes.push_back(token.substr(1));
        }
        // Validate every token even if an earlier wildcard or suffix matched.
        if (separator == std::wstring::npos) break;
        start = separator + 1;
    }
    return S_OK;
}

HRESULT ValidateFixture(const wchar_t *path) {
    // A deliberately small trusted-fixture contract: no relative path, UNC,
    // device namespace, alternate stream, wildcard, directory or leaf reparse.
    if (!path || !path[0] || !path[1] || !path[2]) return E_INVALIDARG;
    const bool drive = (path[0] >= L'A' && path[0] <= L'Z') || (path[0] >= L'a' && path[0] <= L'z');
    if (!drive || path[1] != L':' || path[2] != L'\\') return E_INVALIDARG;
    if (wcspbrk(path + 2, L":*?")) return E_INVALIDARG;
    wchar_t root[] = {path[0], L':', L'\\', 0};
    const UINT type = GetDriveTypeW(root);
    if (type != DRIVE_FIXED && type != DRIVE_REMOVABLE && type != DRIVE_RAMDISK) return E_INVALIDARG;
    const DWORD attributes = GetFileAttributesW(path);
    if (attributes == INVALID_FILE_ATTRIBUTES) return HRESULT_FROM_WIN32(GetLastError());
    if (attributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT)) return E_INVALIDARG;
    return S_OK;
}

class FileOpenProxy final : public IFileOpenDialog {
    enum class Phase { ready, showing, succeeded, failed };
    struct Subscription { DWORD cookie; IFileDialogEvents *sink; };
    struct HoldSelf {
        FileOpenProxy *value;
        explicit HoldSelf(FileOpenProxy *v) : value(v) { value->AddRef(); }
        ~HoldSelf() { value->Release(); }
    };
    struct Snapshot {
        std::vector<Subscription> entries;
        explicit Snapshot(const std::vector<Subscription> &v) : entries(v) { for (const auto &s : entries) s.sink->AddRef(); }
        ~Snapshot() { for (const auto &s : entries) s.sink->Release(); }
    };
    struct CallbackScope {
        bool &active;
        explicit CallbackScope(bool &value) : active(value) { active = true; }
        ~CallbackScope() { active = false; }
    };
    volatile LONG refs_ = 1;
    IFileOpenDialog *real_;
    std::wstring path_;
    bool cancel_only_;
    DWORD thread_ = GetCurrentThreadId();
    ULONGLONG deadline_ = GetTickCount64() + WXBG_FILE_PROXY_LEASE_MS;
    Phase phase_ = Phase::ready;
    HRESULT failure_ = E_UNEXPECTED;
    IShellItemArray *results_ = nullptr;
    std::vector<Subscription> subscriptions_;
    DWORD next_cookie_ = 1;
    bool inside_callback_ = false, callback_invoked_ = false, callback_read_results_ = false;
    std::vector<OwnedFilter> filters_;
    HRESULT filter_error_ = S_OK;
    WxBgFileProxyGrant grant_{};
    bool has_grant_ = false;

    ~FileOpenProxy() {
        ClearResults();
        for (const auto &s : subscriptions_) s.sink->Release();
        real_->Release();
        InterlockedDecrement(&live_objects);
    }
    HRESULT CheckThread() const { return GetCurrentThreadId() == thread_ ? S_OK : RPC_E_WRONG_THREAD; }
    bool Expired() const { return GetTickCount64() >= deadline_; }
    bool Authorized() const noexcept {
        if (!has_grant_) return true;
        if (GetTickCount64() >= grant_.deadline_tick) return false;
        try {
            const BOOL allowed = grant_.authorize(grant_.context, grant_.generation);
            return allowed && GetTickCount64() < grant_.deadline_tick;
        } catch (...) { return false; }
    }
    void ClearResults() { if (results_) { auto *old = results_; results_ = nullptr; old->Release(); } }
    HRESULT Fail(HRESULT hr) { phase_ = Phase::failed; failure_ = FAILED(hr) ? hr : cancelled; ClearResults(); return failure_; }
    HRESULT CanReadResult() {
        const HRESULT thread = CheckThread(); if (FAILED(thread)) return thread;
        if (!results_ || (phase_ != Phase::showing && phase_ != Phase::succeeded)) return E_UNEXPECTED;
        if (!Authorized()) return Fail(cancelled);
        if (phase_ == Phase::showing && Expired()) return timed_out;
        return S_OK;
    }
    HRESULT CopyResults(IShellItemArray **out) {
        if (!out) return E_POINTER;
        *out = nullptr;
        const HRESULT hr = CanReadResult(); if (FAILED(hr)) return hr;
        results_->AddRef(); *out = results_;
        return S_OK;
    }
    HRESULT FilterConfigurationError(HRESULT hr) {
        // A rejected reconfiguration does not overwrite a previously accepted
        // native filter set. With no valid set, do not silently ignore failure.
        if (filters_.empty()) filter_error_ = hr;
        return hr;
    }
    HRESULT CheckStrictFilter(FILEOPENDIALOGOPTIONS options) {
        if (FAILED(filter_error_)) return filter_error_;
        if (!(options & FOS_STRICTFILETYPES)) return S_OK;
        if (filters_.empty()) return E_INVALIDARG;
        UINT index = 0;
        const HRESULT hr = real_->GetFileTypeIndex(&index);
        if (FAILED(hr)) return hr;
        if (!index || index > filters_.size()) return E_INVALIDARG;
        const OwnedFilter &filter = filters_[index - 1];
        if (filter.match_all) return S_OK;
        const size_t name_start = path_.find_last_of(L'\\') + 1;
        const size_t name_length = path_.size() - name_start;
        for (const auto &suffix : filter.suffixes) {
            if (name_length >= suffix.size() && CompareStringOrdinal(
                    path_.data() + path_.size() - suffix.size(), static_cast<int>(suffix.size()),
                    suffix.c_str(), static_cast<int>(suffix.size()), TRUE) == CSTR_EQUAL) return S_OK;
        }
        return E_INVALIDARG;
    }
public:
    FileOpenProxy(IFileOpenDialog *real, const wchar_t *path, const WxBgFileProxyGrant *grant)
        : real_(real), path_(path ? path : L""), cancel_only_(path == nullptr),
          grant_(grant ? *grant : WxBgFileProxyGrant{}), has_grant_(grant != nullptr && path != nullptr) {
        real_->AddRef();
        InterlockedIncrement(&live_objects);
    }
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void **out) override {
        if (!out) return E_POINTER;
        *out = nullptr;
        if (iid == IID_IUnknown || iid == IID_IModalWindow || iid == IID_IFileDialog || iid == IID_IFileOpenDialog) {
            *out = static_cast<IFileOpenDialog *>(this); AddRef(); return S_OK;
        }
        // Never forward unknown QI: that would escape the proxy and expose the
        // real dialog's Show through another interface (including IFileDialog2).
        return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return static_cast<ULONG>(InterlockedIncrement(&refs_)); }
    ULONG STDMETHODCALLTYPE Release() override {
        const ULONG count = static_cast<ULONG>(InterlockedDecrement(&refs_));
        if (!count) delete this;
        return count;
    }
    HRESULT STDMETHODCALLTYPE Show(HWND owner) override {
        (void)owner;
        const HRESULT thread = CheckThread(); if (FAILED(thread)) return thread;
        if (phase_ == Phase::failed) return failure_;
        if (phase_ != Phase::ready) return E_UNEXPECTED;
        if (cancel_only_) {
            const HRESULT result = Fail(cancelled);
            InterlockedIncrement(&cancelled_shows); // Publish after cancellation state is committed.
            return result;
        }
        if (!Authorized()) return Fail(cancelled);
        if (Expired()) return Fail(timed_out);
        HoldSelf hold(this); // Event sinks may release their own proxy reference.
        phase_ = Phase::showing;
        FILEOPENDIALOGOPTIONS options = 0;
        HRESULT hr = real_->GetOptions(&options);
        if (FAILED(hr)) return Fail(hr);
        if (options & FOS_PICKFOLDERS) return Fail(HRESULT_FROM_WIN32(ERROR_NOT_SUPPORTED));
        hr = CheckStrictFilter(options);
        if (FAILED(hr)) return Fail(hr);
        if (subscriptions_.empty()) return Fail(E_UNEXPECTED);
        hr = ValidateFixture(path_.c_str());
        if (FAILED(hr)) return Fail(hr);
        if (!Authorized()) return Fail(cancelled);
        IShellItem *item = nullptr;
        hr = SHCreateItemFromParsingName(path_.c_str(), nullptr, IID_IShellItem, reinterpret_cast<void **>(&item));
        if (FAILED(hr)) return Fail(hr);
        if (!Authorized()) { item->Release(); return Fail(cancelled); }
        hr = SHCreateShellItemArrayFromShellItem(item, IID_IShellItemArray, reinterpret_cast<void **>(&results_));
        item->Release();
        if (FAILED(hr)) return Fail(hr);
        if (!Authorized()) return Fail(cancelled);
        if (Expired()) return Fail(timed_out);
        try {
            Snapshot snapshot(subscriptions_);
            for (const auto &s : snapshot.entries) {
                // An earlier callback may have unadvised this or another sink.
                if (std::none_of(subscriptions_.begin(), subscriptions_.end(),
                    [&](const Subscription &current) { return current.cookie == s.cookie; })) continue;
                if (!Authorized()) return Fail(cancelled);
                if (Expired()) return Fail(timed_out);
                {
                    CallbackScope scope(inside_callback_);
                    callback_invoked_ = true;
                    hr = s.sink->OnFileOk(static_cast<IFileDialog *>(this));
                }
                if (phase_ == Phase::failed) return failure_; // Close overrides callback acceptance.
                if (!Authorized()) return Fail(cancelled);
                if (Expired()) return Fail(timed_out);
                if (hr != S_OK) return Fail(FAILED(hr) ? hr : cancelled);
            }
        } catch (const std::bad_alloc &) { return Fail(E_OUTOFMEMORY); }
          catch (...) { return Fail(E_UNEXPECTED); }
        if (phase_ == Phase::failed) return failure_;
        if (!Authorized()) return Fail(cancelled);
        if (Expired()) return Fail(timed_out);
        if (!callback_invoked_ || !callback_read_results_) return Fail(E_UNEXPECTED);
        phase_ = Phase::succeeded;
        InterlockedIncrement(&accepted_shows);
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE SetFileTypes(UINT count, const COMDLG_FILTERSPEC *specs) override {
        const HRESULT thread = CheckThread(); if (FAILED(thread)) return thread;
        if (cancel_only_) return real_->SetFileTypes(count, specs);
        if (phase_ != Phase::ready) return E_UNEXPECTED;
        if (!count || count > 256 || !specs) return FilterConfigurationError(E_INVALIDARG);
        try {
            std::vector<OwnedFilter> proposed;
            proposed.reserve(count);
            for (UINT i = 0; i < count; ++i) {
                OwnedFilter filter;
                HRESULT hr = CopyFilterText(specs[i].pszName, filter.name);
                if (FAILED(hr)) return FilterConfigurationError(hr);
                hr = CopyFilterText(specs[i].pszSpec, filter.pattern);
                if (FAILED(hr)) return FilterConfigurationError(hr);
                hr = ParseFilter(filter);
                if (FAILED(hr)) return FilterConfigurationError(hr);
                proposed.push_back(std::move(filter));
            }
            const HRESULT hr = real_->SetFileTypes(count, specs);
            if (FAILED(hr)) return FilterConfigurationError(hr);
            filters_.swap(proposed);
            filter_error_ = S_OK;
            return hr;
        } catch (const std::bad_alloc &) { return FilterConfigurationError(E_OUTOFMEMORY); }
    }
    HRESULT STDMETHODCALLTYPE Advise(IFileDialogEvents *sink, DWORD *cookie) override {
        if (!cookie) return E_POINTER;
        *cookie = 0;
        if (!sink) return E_POINTER;
        const HRESULT thread = CheckThread(); if (FAILED(thread)) return thread;
        if (phase_ != Phase::ready || next_cookie_ == 0) return E_UNEXPECTED;
        try { subscriptions_.push_back({next_cookie_, sink}); }
        catch (const std::bad_alloc &) { return E_OUTOFMEMORY; }
        sink->AddRef(); *cookie = next_cookie_++;
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE Unadvise(DWORD cookie) override {
        const HRESULT thread = CheckThread(); if (FAILED(thread)) return thread;
        const auto found = std::find_if(subscriptions_.begin(), subscriptions_.end(),
            [&](const Subscription &s) { return s.cookie == cookie; });
        if (found == subscriptions_.end()) return E_INVALIDARG;
        auto *sink = found->sink;
        subscriptions_.erase(found);
        sink->Release();
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE Close(HRESULT hr) override {
        const HRESULT thread = CheckThread(); if (FAILED(thread)) return thread;
        if (phase_ == Phase::succeeded) return E_UNEXPECTED;
        if (phase_ != Phase::failed) Fail(hr);
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE GetResults(IShellItemArray **out) override {
        const HRESULT hr = CopyResults(out);
        if (SUCCEEDED(hr) && inside_callback_) {
            callback_read_results_ = true;
            InterlockedIncrement(&callback_result_reads);
        }
        return hr;
    }
    HRESULT STDMETHODCALLTYPE GetResult(IShellItem **out) override {
        if (!out) return E_POINTER;
        *out = nullptr;
        const HRESULT hr = CanReadResult(); if (FAILED(hr)) return hr;
        return results_->GetItemAt(0, out);
    }
    HRESULT STDMETHODCALLTYPE GetSelectedItems(IShellItemArray **out) override { return CopyResults(out); }
    HRESULT STDMETHODCALLTYPE GetCurrentSelection(IShellItem **out) override { return GetResult(out); }

    // Configuration and its getters stay native, but never escape creator thread.
#define DELEGATE(name, signature, args) HRESULT STDMETHODCALLTYPE name signature override { const HRESULT h=CheckThread(); return FAILED(h)?h:real_->name args; }
    DELEGATE(SetFileTypeIndex, (UINT n), (n))
    DELEGATE(GetFileTypeIndex, (UINT *n), (n))
    DELEGATE(SetOptions, (FILEOPENDIALOGOPTIONS v), (v))
    DELEGATE(GetOptions, (FILEOPENDIALOGOPTIONS *v), (v))
    DELEGATE(SetDefaultFolder, (IShellItem *v), (v))
    DELEGATE(SetFolder, (IShellItem *v), (v))
    DELEGATE(GetFolder, (IShellItem **v), (v))
    DELEGATE(SetFileName, (LPCWSTR v), (v))
    DELEGATE(GetFileName, (LPWSTR *v), (v))
    DELEGATE(SetTitle, (LPCWSTR v), (v))
    DELEGATE(SetOkButtonLabel, (LPCWSTR v), (v))
    DELEGATE(SetFileNameLabel, (LPCWSTR v), (v))
    DELEGATE(AddPlace, (IShellItem *v, FDAP a), (v,a))
    DELEGATE(SetDefaultExtension, (LPCWSTR v), (v))
    DELEGATE(SetClientGuid, (REFGUID v), (v))
    DELEGATE(ClearClientData, (), ())
    DELEGATE(SetFilter, (IShellItemFilter *v), (v))
#undef DELEGATE
};
} // namespace

static HRESULT CreateProxy(IFileOpenDialog *borrowed_real, const wchar_t *fixture_path,
                           const WxBgFileProxyGrant *grant, IFileOpenDialog **out) {
    if (!out) return E_POINTER;
    *out = nullptr;
    if (!borrowed_real) return E_POINTER;
    ULONG_PTR context = 0;
    HRESULT hr = CoGetContextToken(&context);
    if (FAILED(hr)) return hr;
    if (fixture_path) { hr = ValidateFixture(fixture_path); if (FAILED(hr)) return hr; }
    try { *out = new FileOpenProxy(borrowed_real, fixture_path, grant); }
    catch (const std::bad_alloc &) { return E_OUTOFMEMORY; }
    return S_OK;
}

HRESULT CreateWxBgFileOpenProxy(IFileOpenDialog *borrowed_real, const wchar_t *fixture_path, IFileOpenDialog **out) {
    return CreateProxy(borrowed_real, fixture_path, nullptr, out);
}

HRESULT CreateWxBgFileOpenProxyWithGrant(IFileOpenDialog *borrowed_real, const wchar_t *fixture_path,
                                        const WxBgFileProxyGrant *grant, IFileOpenDialog **out) {
    if (!out) return E_POINTER;
    *out = nullptr;
    if (!borrowed_real) return E_POINTER;
    if (fixture_path && (!grant || !grant->generation || !grant->deadline_tick || !grant->authorize)) return E_INVALIDARG;
    return CreateProxy(borrowed_real, fixture_path, fixture_path ? grant : nullptr, out);
}
