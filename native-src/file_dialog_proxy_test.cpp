#include "file_dialog_proxy.h"
#include <shlobj.h>
#include <atomic>
#include <cstdio>
#include <string>
#include <vector>

static const HRESULT CANCELLED = HRESULT_FROM_WIN32(ERROR_CANCELLED);
static int failed_checks = 0;
#define CHECK(x) do { if (!(x)) { std::printf("FAIL line=%d: %s\n", __LINE__, #x); ++failed_checks; return false; } } while (0)
template<class T> struct Ref {
    T *p = nullptr;
    ~Ref() { if (p) p->Release(); }
    T **put() { return &p; }
    T *operator->() const { return p; }
};
struct Counts { LONG refs = 0; int destroyed = 0, show = 0, close = 0, title = 0, advise = 0; HRESULT options_error = S_OK; bool override_index = false; UINT forced_index = 0; };

// Owns a genuine native dialog, but its Show deliberately fails if ever called.
// This measures proxy delegation without allowing an accidental test UI.
class RecordingDialog final : public IFileOpenDialog {
    IFileOpenDialog *real_; Counts &counts_;
    ~RecordingDialog() { real_->Release(); ++counts_.destroyed; }
public:
    RecordingDialog(IFileOpenDialog *real, Counts &counts) : real_(real), counts_(counts) { real_->AddRef(); counts_.refs = 1; }
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void **out) override {
        if (!out) return E_POINTER; *out = nullptr;
        if (iid == IID_IUnknown || iid == IID_IModalWindow || iid == IID_IFileDialog || iid == IID_IFileOpenDialog) {
            *out = static_cast<IFileOpenDialog *>(this); AddRef(); return S_OK;
        }
        return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return InterlockedIncrement(&counts_.refs); }
    ULONG STDMETHODCALLTYPE Release() override { ULONG n = InterlockedDecrement(&counts_.refs); if (!n) delete this; return n; }
    HRESULT STDMETHODCALLTYPE Show(HWND) override { ++counts_.show; return E_ACCESSDENIED; }
    HRESULT STDMETHODCALLTYPE Close(HRESULT) override { ++counts_.close; return E_ACCESSDENIED; }
    HRESULT STDMETHODCALLTYPE SetTitle(LPCWSTR s) override { ++counts_.title; return real_->SetTitle(s); }
    HRESULT STDMETHODCALLTYPE Advise(IFileDialogEvents *s, DWORD *c) override { ++counts_.advise; return real_->Advise(s,c); }
    HRESULT STDMETHODCALLTYPE GetOptions(FILEOPENDIALOGOPTIONS *v) override { return FAILED(counts_.options_error) ? counts_.options_error : real_->GetOptions(v); }
    HRESULT STDMETHODCALLTYPE GetFileTypeIndex(UINT *v) override { if(counts_.override_index){if(!v)return E_POINTER;*v=counts_.forced_index;return S_OK;}return real_->GetFileTypeIndex(v); }
#define FORWARD(name, signature, args) HRESULT STDMETHODCALLTYPE name signature override { return real_->name args; }
    FORWARD(SetFileTypes, (UINT n, const COMDLG_FILTERSPEC *v), (n,v))
    FORWARD(SetFileTypeIndex, (UINT n), (n))
    FORWARD(Unadvise, (DWORD n), (n))
    FORWARD(SetOptions, (FILEOPENDIALOGOPTIONS v), (v))
    FORWARD(SetDefaultFolder, (IShellItem *v), (v))
    FORWARD(SetFolder, (IShellItem *v), (v))
    FORWARD(GetFolder, (IShellItem **v), (v))
    FORWARD(GetCurrentSelection, (IShellItem **v), (v))
    FORWARD(SetFileName, (LPCWSTR v), (v))
    FORWARD(GetFileName, (LPWSTR *v), (v))
    FORWARD(SetOkButtonLabel, (LPCWSTR v), (v))
    FORWARD(SetFileNameLabel, (LPCWSTR v), (v))
    FORWARD(GetResult, (IShellItem **v), (v))
    FORWARD(AddPlace, (IShellItem *v, FDAP a), (v,a))
    FORWARD(SetDefaultExtension, (LPCWSTR v), (v))
    FORWARD(SetClientGuid, (REFGUID v), (v))
    FORWARD(ClearClientData, (), ())
    FORWARD(SetFilter, (IShellItemFilter *v), (v))
    FORWARD(GetResults, (IShellItemArray **v), (v))
    FORWARD(GetSelectedItems, (IShellItemArray **v), (v))
#undef FORWARD
};

struct SinkStats { LONG refs = 0; int destroyed = 0, calls = 0; bool read_ok = false; };
class Sink final : public IFileDialogEvents {
    SinkStats &stats_;
    std::wstring expected_;
    ~Sink() { ++stats_.destroyed; }
public:
    IFileOpenDialog *saved = nullptr; // Borrowed, deliberately tests Qt's saved pointer.
    HRESULT response = S_OK;
    HRESULT close_with = E_PENDING;
    DWORD unadvise_cookie = 0;
    HRESULT nested_show = S_OK;
    bool try_reentry = false;
    bool read_results = true, selected_items_only = false;
    volatile LONG *revoke_before_read = nullptr, *revoke_after_read = nullptr;
    HRESULT attempted_read = E_PENDING;
    Sink(SinkStats &s, const std::wstring &path) : stats_(s), expected_(path) { stats_.refs = 1; }
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void **out) override {
        if (!out) return E_POINTER; *out = nullptr;
        if (iid == IID_IUnknown || iid == IID_IFileDialogEvents) { *out=static_cast<IFileDialogEvents *>(this); AddRef(); return S_OK; }
        return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return InterlockedIncrement(&stats_.refs); }
    ULONG STDMETHODCALLTYPE Release() override { ULONG n=InterlockedDecrement(&stats_.refs); if (!n) delete this; return n; }
    HRESULT STDMETHODCALLTYPE OnFileOk(IFileDialog *dialog) override {
        ++stats_.calls;
        if (!read_results) return response;
        if(revoke_before_read)InterlockedExchange(revoke_before_read,1);
        Ref<IShellItemArray> array; Ref<IShellItem> item; DWORD count=0; LPWSTR path=nullptr;
        HRESULT hr=selected_items_only?saved->GetSelectedItems(array.put()):saved->GetResults(array.put());
        attempted_read=hr;
        if (SUCCEEDED(hr)) hr=array->GetCount(&count);
        if (SUCCEEDED(hr) && count==1) hr=array->GetItemAt(0,item.put()); else hr=E_FAIL;
        if (SUCCEEDED(hr)) hr=item->GetDisplayName(SIGDN_FILESYSPATH,&path);
        stats_.read_ok=SUCCEEDED(hr) && path && _wcsicmp(path,expected_.c_str())==0;
        CoTaskMemFree(path);
        if(revoke_after_read)InterlockedExchange(revoke_after_read,1);
        if (dialog != static_cast<IFileDialog *>(saved)) stats_.read_ok=false;
        if (try_reentry) nested_show=saved->Show(nullptr);
        if (unadvise_cookie) saved->Unadvise(unadvise_cookie);
        if (close_with!=E_PENDING) saved->Close(close_with);
        return response;
    }
    HRESULT STDMETHODCALLTYPE OnFolderChanging(IFileDialog *,IShellItem *) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE OnFolderChange(IFileDialog *) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE OnSelectionChange(IFileDialog *) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE OnShareViolation(IFileDialog *,IShellItem *,FDE_SHAREVIOLATION_RESPONSE *v) override { if(v)*v=FDESVR_DEFAULT;return S_OK; }
    HRESULT STDMETHODCALLTYPE OnTypeChange(IFileDialog *) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE OnOverwrite(IFileDialog *,IShellItem *,FDE_OVERWRITE_RESPONSE *v) override { if(v)*v=FDEOR_DEFAULT;return S_OK; }
};

struct Case {
    Counts counts;
    Ref<IFileOpenDialog> native, real, proxy;
    bool create(const wchar_t *path,const WxBgFileProxyGrant *grant=nullptr) {
        CHECK(SUCCEEDED(CoCreateInstance(CLSID_FileOpenDialog,nullptr,CLSCTX_INPROC_SERVER,IID_IFileOpenDialog,reinterpret_cast<void **>(native.put()))));
        real.p=new RecordingDialog(native.p,counts);
        CHECK((grant?CreateWxBgFileOpenProxyWithGrant(real.p,path,grant,proxy.put()):CreateWxBgFileOpenProxy(real.p,path,proxy.put()))==S_OK);
        CHECK(counts.refs==2);
        return true;
    }
};
static bool success_case(const std::wstring &path) {
    const LONG cancellations=WxBgFileOpenProxyCancelledShowCount();
    const LONG accepted=WxBgFileOpenProxyAcceptedShowCount(),reads=WxBgFileOpenProxyCallbackGetResultsCount();
    SinkStats stats; Case c; CHECK(c.create(path.c_str())); Ref<Sink> sink; sink.p=new Sink(stats,path);sink->saved=c.proxy.p;DWORD cookie=0;
    CHECK(c.proxy->Advise(sink.p,&cookie)==S_OK && cookie!=0 && stats.refs==2);
    Ref<IShellItemArray> before;CHECK(FAILED(c.proxy->GetResults(before.put())) && !before.p);
    CHECK(c.proxy->Show(nullptr)==S_OK);CHECK(stats.calls==1 && stats.read_ok);
    CHECK(WxBgFileOpenProxyAcceptedShowCount()==accepted+1 && WxBgFileOpenProxyCallbackGetResultsCount()==reads+1);
    Ref<IShellItem> one;CHECK(c.proxy->GetResult(one.put())==S_OK && one.p);
    Ref<IShellItemArray> selected;CHECK(c.proxy->GetSelectedItems(selected.put())==S_OK);
    CHECK(c.proxy->Unadvise(cookie)==S_OK && stats.refs==1);
    CHECK(FAILED(c.proxy->Unadvise(cookie)));
    CHECK(FAILED(c.proxy->Show(nullptr)));
    CHECK(c.counts.show==0 && c.counts.close==0 && c.counts.advise==0);
    CHECK(WxBgFileOpenProxyCancelledShowCount()==cancellations);
    CHECK(WxBgFileOpenProxyAcceptedShowCount()==accepted+1 && WxBgFileOpenProxyCallbackGetResultsCount()==reads+1);
    return true;
}
static bool qi_and_configuration(const std::wstring &path) {
    Case c;CHECK(c.create(path.c_str()));
    for (const IID *iid : {&IID_IUnknown,&IID_IModalWindow,&IID_IFileDialog,&IID_IFileOpenDialog}) {
        IUnknown *p=nullptr,*identity=nullptr;CHECK(c.proxy->QueryInterface(*iid,reinterpret_cast<void **>(&p))==S_OK);
        CHECK(p->QueryInterface(IID_IUnknown,reinterpret_cast<void **>(&identity))==S_OK);
        CHECK(identity==static_cast<IUnknown *>(c.proxy.p)); identity->Release();p->Release();
    }
    void *out=reinterpret_cast<void *>(1);
    CHECK(c.proxy->QueryInterface(IID_IFileDialog2,&out)==E_NOINTERFACE && out==nullptr);
    CHECK(c.proxy->QueryInterface(IID_IFileDialogCustomize,&out)==E_NOINTERFACE && out==nullptr);
    CHECK(c.proxy->QueryInterface(IID_IUnknown,nullptr)==E_POINTER);
    CHECK(c.proxy->SetTitle(L"Invisible owned diagnostic")==S_OK && c.counts.title==1);
    FILEOPENDIALOGOPTIONS options=0;CHECK(c.proxy->GetOptions(&options)==S_OK);
    CHECK(c.proxy->SetOptions(options|FOS_ALLOWMULTISELECT|FOS_FILEMUSTEXIST)==S_OK);
    FILEOPENDIALOGOPTIONS actual=0;CHECK(c.real->GetOptions(&actual)==S_OK && (actual&FOS_ALLOWMULTISELECT));
    return true;
}
static bool callback_failure(const std::wstring &path,HRESULT response,HRESULT close_with) {
    const LONG cancellations=WxBgFileOpenProxyCancelledShowCount();
    const LONG accepted=WxBgFileOpenProxyAcceptedShowCount(),reads=WxBgFileOpenProxyCallbackGetResultsCount();
    SinkStats stats;Case c;CHECK(c.create(path.c_str()));Ref<Sink> sink;sink.p=new Sink(stats,path);sink->saved=c.proxy.p;sink->response=response;sink->close_with=close_with;DWORD cookie;
    CHECK(c.proxy->Advise(sink.p,&cookie)==S_OK);
    HRESULT expected=close_with==E_PENDING ? (response==S_FALSE?CANCELLED:response) : (FAILED(close_with)?close_with:CANCELLED);
    CHECK(c.proxy->Show(nullptr)==expected);CHECK(stats.calls==1 && stats.read_ok);
    Ref<IShellItemArray> result;CHECK(FAILED(c.proxy->GetResults(result.put())) && !result.p);
    CHECK(c.proxy->Unadvise(cookie)==S_OK && stats.refs==1);CHECK(c.counts.show==0 && c.counts.close==0);
    CHECK(WxBgFileOpenProxyCancelledShowCount()==cancellations);
    CHECK(WxBgFileOpenProxyAcceptedShowCount()==accepted && WxBgFileOpenProxyCallbackGetResultsCount()==reads+1);
    return true;
}
static bool ownership_and_reentry(const std::wstring &path) {
    Counts counts;SinkStats stats;Ref<IFileOpenDialog> native;
    CHECK(SUCCEEDED(CoCreateInstance(CLSID_FileOpenDialog,nullptr,CLSCTX_INPROC_SERVER,IID_IFileOpenDialog,reinterpret_cast<void **>(native.put()))));
    auto *real=new RecordingDialog(native.p,counts);IFileOpenDialog *proxy=nullptr;
    CHECK(CreateWxBgFileOpenProxy(real,path.c_str(),&proxy)==S_OK);real->Release();CHECK(counts.refs==1);
    auto *sink=new Sink(stats,path);sink->saved=proxy;sink->try_reentry=true;DWORD cookie;
    CHECK(proxy->Advise(sink,&cookie)==S_OK);sink->unadvise_cookie=cookie;sink->Release();
    CHECK(proxy->Show(nullptr)==S_OK);CHECK(stats.calls==1 && stats.read_ok && stats.destroyed==1);
    // Nested Show is checked by a second sink that keeps its caller reference.
    proxy->Release();CHECK(counts.refs==0 && counts.destroyed==1 && counts.show==0);
    SinkStats second;Case c;CHECK(c.create(path.c_str()));Ref<Sink> keep;keep.p=new Sink(second,path);keep->saved=c.proxy.p;keep->try_reentry=true;
    CHECK(c.proxy->Advise(keep.p,&cookie)==S_OK);CHECK(c.proxy->Show(nullptr)==S_OK);CHECK(FAILED(keep->nested_show));
    CHECK(c.proxy->Unadvise(cookie)==S_OK);return true;
}
static bool destructor_unadvises(const std::wstring &path) {
    SinkStats stats;Case c;CHECK(c.create(path.c_str()));auto *sink=new Sink(stats,path);DWORD cookie;
    CHECK(c.proxy->Advise(sink,&cookie)==S_OK);sink->Release();CHECK(stats.refs==1);
    c.proxy.p->Release();c.proxy.p=nullptr;CHECK(stats.refs==0 && stats.destroyed==1 && c.counts.refs==1);return true;
}
struct CrossThread { IFileOpenDialog *proxy; HRESULT result=E_PENDING; };
static DWORD WINAPI wrong_thread(void *raw) { auto *x=static_cast<CrossThread *>(raw);x->result=x->proxy->Show(nullptr);return 0; }
static bool wrong_thread_case(const std::wstring &path) {
    SinkStats stats;Case c;CHECK(c.create(path.c_str()));Ref<Sink> sink;sink.p=new Sink(stats,path);sink->saved=c.proxy.p;DWORD cookie;
    CHECK(c.proxy->Advise(sink.p,&cookie)==S_OK);
    CrossThread x{c.proxy.p};HANDLE h=CreateThread(nullptr,0,wrong_thread,&x,0,nullptr);CHECK(h);
    CHECK(WaitForSingleObject(h,3000)==WAIT_OBJECT_0);CloseHandle(h);CHECK(x.result==RPC_E_WRONG_THREAD);
    CHECK(c.proxy->Show(nullptr)==S_OK);CHECK(c.proxy->Unadvise(cookie)==S_OK);return true;
}
static bool expired_case(const std::wstring &path) {
    Case c;CHECK(c.create(path.c_str()));Sleep(static_cast<DWORD>(WXBG_FILE_PROXY_LEASE_MS+30));
    CHECK(c.proxy->Show(nullptr)==HRESULT_FROM_WIN32(ERROR_TIMEOUT));Ref<IShellItemArray> result;CHECK(FAILED(c.proxy->GetResults(result.put())));return true;
}
static bool cancel_only(const std::wstring &path) {
    const LONG cancellations=WxBgFileOpenProxyCancelledShowCount();
    const LONG accepted=WxBgFileOpenProxyAcceptedShowCount(),reads=WxBgFileOpenProxyCallbackGetResultsCount();
    SinkStats stats;Case c;CHECK(c.create(nullptr));Ref<Sink> sink;sink.p=new Sink(stats,path);sink->saved=c.proxy.p;DWORD cookie;
    // Cancel-only never consults selection options or prepares a fixture.
    CHECK(c.proxy->SetOptions(FOS_PICKFOLDERS)==S_OK);c.counts.options_error=E_ACCESSDENIED;
    COMDLG_FILTERSPEC irrelevant={L"Not used by cancellation",L"a?b.*"};c.proxy->SetFileTypes(1,&irrelevant);
    CHECK(c.proxy->Advise(sink.p,&cookie)==S_OK);CHECK(WxBgFileOpenProxyCancelledShowCount()==cancellations);
    CrossThread x{c.proxy.p};HANDLE h=CreateThread(nullptr,0,wrong_thread,&x,0,nullptr);CHECK(h);
    CHECK(WaitForSingleObject(h,3000)==WAIT_OBJECT_0);CloseHandle(h);CHECK(x.result==RPC_E_WRONG_THREAD);
    CHECK(WxBgFileOpenProxyCancelledShowCount()==cancellations);
    CHECK(c.proxy->Show(nullptr)==CANCELLED);CHECK(stats.calls==0);
    CHECK(WxBgFileOpenProxyCancelledShowCount()==cancellations+1);
    CHECK(c.proxy->Show(nullptr)==CANCELLED);
    CHECK(WxBgFileOpenProxyCancelledShowCount()==cancellations+1);
    Ref<IShellItemArray> result;CHECK(FAILED(c.proxy->GetResults(result.put())) && !result.p);CHECK(c.counts.show==0);
    CHECK(WxBgFileOpenProxyAcceptedShowCount()==accepted && WxBgFileOpenProxyCallbackGetResultsCount()==reads);
    CHECK(c.proxy->Unadvise(cookie)==S_OK);return true;
}
static bool invalid_arguments(const std::wstring &path) {
    CHECK(CreateWxBgFileOpenProxy(nullptr,path.c_str(),nullptr)==E_POINTER);
    IFileOpenDialog *out=reinterpret_cast<IFileOpenDialog *>(1);
    CHECK(FAILED(CreateWxBgFileOpenProxy(nullptr,path.c_str(),&out)) && !out);
    Case c;CHECK(c.create(path.c_str()));
    CHECK(FAILED(CreateWxBgFileOpenProxy(c.real.p,L"relative.txt",&out)) && !out);
    std::wstring missing=path+L".missing";CHECK(FAILED(CreateWxBgFileOpenProxy(c.real.p,missing.c_str(),&out)) && !out);
    CHECK(c.proxy->GetResults(nullptr)==E_POINTER);CHECK(c.proxy->GetResult(nullptr)==E_POINTER);
    CHECK(c.proxy->Advise(nullptr,nullptr)==E_POINTER);return true;
}
static bool live_object_counter(const std::wstring &path) {
    LONG before=WxBgFileOpenProxyLiveCount();CHECK(before==0);
    Case c;CHECK(c.create(path.c_str()));CHECK(WxBgFileOpenProxyLiveCount()==before+1);
    Ref<IUnknown> identity;CHECK(c.proxy->QueryInterface(IID_IUnknown,reinterpret_cast<void **>(identity.put()))==S_OK);
    CHECK(WxBgFileOpenProxyLiveCount()==before+1);
    c.proxy.p->Release();c.proxy.p=nullptr;CHECK(WxBgFileOpenProxyLiveCount()==before+1);
    identity.p->Release();identity.p=nullptr;CHECK(WxBgFileOpenProxyLiveCount()==before);return true;
}
static bool missing_sink_rejected(const std::wstring &path) {
    Case c;CHECK(c.create(path.c_str()));CHECK(c.proxy->Show(nullptr)==E_UNEXPECTED);
    Ref<IShellItemArray> result;CHECK(FAILED(c.proxy->GetResults(result.put())) && !result.p);return true;
}
static bool unread_results_rejected(const std::wstring &path,bool selected_only) {
    const LONG accepted=WxBgFileOpenProxyAcceptedShowCount(),reads=WxBgFileOpenProxyCallbackGetResultsCount();
    SinkStats stats;Case c;CHECK(c.create(path.c_str()));Ref<Sink> sink;sink.p=new Sink(stats,path);sink->saved=c.proxy.p;
    sink->read_results=selected_only;sink->selected_items_only=selected_only;DWORD cookie;
    CHECK(c.proxy->Advise(sink.p,&cookie)==S_OK);CHECK(c.proxy->Show(nullptr)==E_UNEXPECTED);
    CHECK(stats.calls==1);CHECK(stats.read_ok==selected_only);
    CHECK(WxBgFileOpenProxyAcceptedShowCount()==accepted && WxBgFileOpenProxyCallbackGetResultsCount()==reads);
    Ref<IShellItemArray> result;CHECK(FAILED(c.proxy->GetResults(result.put())) && !result.p);
    CHECK(c.proxy->Unadvise(cookie)==S_OK);return true;
}
static bool incompatible_options_rejected(const std::wstring &path,bool getter_error) {
    SinkStats stats;Case c;CHECK(c.create(path.c_str()));Ref<Sink> sink;sink.p=new Sink(stats,path);sink->saved=c.proxy.p;DWORD cookie;
    CHECK(c.proxy->Advise(sink.p,&cookie)==S_OK);
    if(getter_error)c.counts.options_error=E_ACCESSDENIED;
    else CHECK(c.proxy->SetOptions(FOS_PICKFOLDERS)==S_OK);
    CHECK(c.proxy->Show(nullptr)==(getter_error?E_ACCESSDENIED:HRESULT_FROM_WIN32(ERROR_NOT_SUPPORTED)));
    CHECK(stats.calls==0);Ref<IShellItemArray> result;CHECK(FAILED(c.proxy->GetResults(result.put())) && !result.p);
    CHECK(c.proxy->Unadvise(cookie)==S_OK);return true;
}
static bool filter_case(const std::wstring &path,std::vector<std::wstring> patterns,UINT index,bool strict,HRESULT expected_set,HRESULT expected_show,bool mutate=false) {
    const LONG accepted=WxBgFileOpenProxyAcceptedShowCount(),reads=WxBgFileOpenProxyCallbackGetResultsCount();
    SinkStats stats;Case c;CHECK(c.create(path.c_str()));Ref<Sink> sink;sink.p=new Sink(stats,path);sink->saved=c.proxy.p;DWORD cookie;
    CHECK(c.proxy->Advise(sink.p,&cookie)==S_OK);FILEOPENDIALOGOPTIONS options=0;CHECK(c.proxy->GetOptions(&options)==S_OK);
    CHECK(c.proxy->SetOptions(strict?(options|FOS_STRICTFILETYPES):(options&~FOS_STRICTFILETYPES))==S_OK);
    if(!patterns.empty()) {
        std::vector<COMDLG_FILTERSPEC> filters;for(const auto &pattern:patterns)filters.push_back({L"Owned filter",pattern.c_str()});
        HRESULT configured=c.proxy->SetFileTypes(static_cast<UINT>(filters.size()),filters.data());CHECK(configured==expected_set);
        if(SUCCEEDED(configured)){if(index)CHECK(c.proxy->SetFileTypeIndex(index)==S_OK);else {c.counts.override_index=true;c.counts.forced_index=0;}}
        if(mutate)for(auto &pattern:patterns)pattern.assign(L"*.md");
    }
    CHECK(c.proxy->Show(nullptr)==expected_show);
    CHECK(stats.calls==(expected_show==S_OK?1:0));
    CHECK(WxBgFileOpenProxyAcceptedShowCount()==accepted+(expected_show==S_OK?1:0));
    CHECK(WxBgFileOpenProxyCallbackGetResultsCount()==reads+(expected_show==S_OK?1:0));
    if(FAILED(expected_show)){Ref<IShellItemArray> result;CHECK(FAILED(c.proxy->GetResults(result.put())) && !result.p);}
    CHECK(c.proxy->Unadvise(cookie)==S_OK);return true;
}
static bool null_filter_arguments(const std::wstring &path) {
    Case c;CHECK(c.create(path.c_str()));
    CHECK(c.proxy->SetFileTypes(1,nullptr)==E_INVALIDARG);
    COMDLG_FILTERSPEC missing={L"Missing",nullptr};CHECK(c.proxy->SetFileTypes(1,&missing)==E_INVALIDARG);
    COMDLG_FILTERSPEC empty={L"Empty",L""};CHECK(c.proxy->SetFileTypes(1,&empty)==E_INVALIDARG);
    COMDLG_FILTERSPEC valid={L"Owned",L"*.txt"};CHECK(c.proxy->SetFileTypes(0,&valid)==E_INVALIDARG);return true;
}
struct GrantState { volatile LONG revoked=0; volatile LONG64 generation=41; };
static BOOL WINAPI authorize_grant(void *raw,ULONGLONG generation) {
    auto *state=static_cast<GrantState *>(raw);
    return state && !InterlockedCompareExchange(&state->revoked,0,0) &&
        static_cast<ULONGLONG>(InterlockedCompareExchange64(&state->generation,0,0))==generation;
}
static bool revocable_grant_case(const std::wstring &path,int mode) {
    const LONG accepted=WxBgFileOpenProxyAcceptedShowCount(),reads=WxBgFileOpenProxyCallbackGetResultsCount();
    SinkStats stats;GrantState state;Case c;
    WxBgFileProxyGrant grant{41,GetTickCount64()+3000,authorize_grant,&state};
    if(mode==2)grant.deadline_tick=GetTickCount64()+40;
    CHECK(c.create(path.c_str(),&grant));
    // The record itself is a copy; the opaque referenced state remains alive.
    grant.generation=999;grant.deadline_tick=0;grant.authorize=nullptr;grant.context=nullptr;
    Ref<Sink> sink;sink.p=new Sink(stats,path);sink->saved=c.proxy.p;DWORD cookie;
    CHECK(c.proxy->Advise(sink.p,&cookie)==S_OK);
    if(mode==1)InterlockedExchange(&state.revoked,1);
    if(mode==2)Sleep(70);
    if(mode==3)InterlockedExchange64(&state.generation,42);
    if(mode==4)sink->revoke_before_read=&state.revoked;
    if(mode==5)sink->revoke_after_read=&state.revoked;
    const HRESULT expected=mode==0?S_OK:CANCELLED;
    CHECK(c.proxy->Show(nullptr)==expected);
    CHECK(stats.calls==((mode==0 || mode>=4)?1:0));
    CHECK(WxBgFileOpenProxyAcceptedShowCount()==accepted+(mode==0?1:0));
    CHECK(WxBgFileOpenProxyCallbackGetResultsCount()==reads+((mode==0 || mode==5)?1:0));
    if(mode==4)CHECK(sink->attempted_read==CANCELLED && !stats.read_ok);
    if(mode==5)CHECK(sink->attempted_read==S_OK && stats.read_ok);
    if(mode!=0){Ref<IShellItemArray> result;CHECK(FAILED(c.proxy->GetResults(result.put())) && !result.p);}
    CHECK(c.proxy->Unadvise(cookie)==S_OK);return true;
}
static bool malformed_grant_rejected(const std::wstring &path) {
    Case c;CHECK(c.create(path.c_str()));IFileOpenDialog *out=nullptr;
    CHECK(CreateWxBgFileOpenProxyWithGrant(c.real.p,path.c_str(),nullptr,&out)==E_INVALIDARG && !out);
    WxBgFileProxyGrant grant{};CHECK(CreateWxBgFileOpenProxyWithGrant(c.real.p,path.c_str(),&grant,&out)==E_INVALIDARG && !out);
    GrantState state;grant={41,GetTickCount64()+3000,authorize_grant,&state};grant.generation=0;
    CHECK(CreateWxBgFileOpenProxyWithGrant(c.real.p,path.c_str(),&grant,&out)==E_INVALIDARG && !out);return true;
}
static bool cancel_ignores_positive_grant(const std::wstring &path) {
    GrantState state;InterlockedExchange(&state.revoked,1);
    WxBgFileProxyGrant grant{999,1,authorize_grant,&state};Case c;CHECK(c.create(nullptr,&grant));
    const LONG cancel=WxBgFileOpenProxyCancelledShowCount(),accepted=WxBgFileOpenProxyAcceptedShowCount();
    CHECK(c.proxy->Show(nullptr)==CANCELLED);CHECK(WxBgFileOpenProxyCancelledShowCount()==cancel+1);
    CHECK(WxBgFileOpenProxyAcceptedShowCount()==accepted);(void)path;return true;
}

struct Monitor { std::atomic<bool> stop{false}; std::atomic<unsigned> samples{0},visible{0},foreground_changes{0};HWND foreground=GetForegroundWindow(); };
static BOOL CALLBACK visible_window(HWND hwnd,LPARAM raw) { DWORD pid=0;GetWindowThreadProcessId(hwnd,&pid);if(pid==GetCurrentProcessId() && IsWindowVisible(hwnd))++static_cast<Monitor *>(reinterpret_cast<void *>(raw))->visible;return TRUE; }
static DWORD WINAPI observe(void *raw) { auto *m=static_cast<Monitor *>(raw);while(!m->stop){EnumWindows(visible_window,reinterpret_cast<LPARAM>(m));if(GetForegroundWindow()!=m->foreground)++m->foreground_changes;++m->samples;Sleep(1);}return 0; }
int main() {
    HRESULT init=CoInitializeEx(nullptr,COINIT_APARTMENTTHREADED|COINIT_DISABLE_OLE1DDE);if(FAILED(init)){std::printf("COM initialization failed\n");return 2;}
    wchar_t exe[32768];DWORD length=GetModuleFileNameW(nullptr,exe,32768);if(!length || length>=32768){CoUninitialize();return 2;}
    std::wstring path(exe,length);path.resize(path.find_last_of(L"\\/")+1);path+=L"file-dialog-owned-fixture.txt";
    HANDLE file=CreateFileW(path.c_str(),GENERIC_WRITE,0,nullptr,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,nullptr);
    if(file==INVALID_HANDLE_VALUE){std::printf("Cannot create unique owned fixture error=%lu\n",GetLastError());CoUninitialize();return 2;}
    const char text[]="wxbg owned fixture\r\n";DWORD written=0;BOOL wrote=WriteFile(file,text,sizeof(text)-1,&written,nullptr);CloseHandle(file);
    if(!wrote || written!=sizeof(text)-1){DeleteFileW(path.c_str());CoUninitialize();return 2;}
    Monitor monitor;HANDLE watcher=CreateThread(nullptr,0,observe,&monitor,0,nullptr);int total=0,passed=0;
#define RUN(label,expr) do { ++total;bool ok=(expr);if(ok)++passed;std::printf("%s %s\n",ok?"PASS":"FAIL",label); } while(0)
    RUN("results_visible_inside_saved_pointer_OnFileOk",success_case(path));
    RUN("COM_identity_and_configuration_delegation",qi_and_configuration(path));
    RUN("veto_clears_results",callback_failure(path,S_FALSE,E_PENDING));
    RUN("sink_error_propagates",callback_failure(path,E_ACCESSDENIED,E_PENDING));
    RUN("Close_cancels_and_clears_results",callback_failure(path,S_OK,CANCELLED));
    RUN("Close_success_does_not_fake_selection",callback_failure(path,S_OK,S_OK));
    RUN("owned_refs_self_unadvise_and_reentry",ownership_and_reentry(path));
    RUN("destructor_releases_unadvised_sink",destructor_unadvises(path));
    RUN("wrong_thread_rejected",wrong_thread_case(path));
    RUN("creation_lease_expired",expired_case(path));
    RUN("null_fixture_cancel_only",cancel_only(path));
    RUN("invalid_arguments_fail_closed",invalid_arguments(path));
    RUN("live_object_counter_tracks_final_release",live_object_counter(path));
    RUN("missing_event_sink_rejected",missing_sink_rejected(path));
    RUN("noop_event_sink_rejected",unread_results_rejected(path,false));
    RUN("selected_items_is_not_GetResults_acceptance",unread_results_rejected(path,true));
    RUN("folder_picker_options_rejected",incompatible_options_rejected(path,false));
    RUN("unreadable_options_fail_closed",incompatible_options_rejected(path,true));
    RUN("strict_exact_filter_match",filter_case(path,{L"*.txt"},1,true,S_OK,S_OK));
    RUN("strict_filter_mismatch_rejected",filter_case(path,{L"*.md"},1,true,S_OK,E_INVALIDARG));
    RUN("strict_semicolon_extension_match",filter_case(path,{L"*.md;*.txt"},1,true,S_OK,S_OK));
    RUN("strict_case_insensitive_match",filter_case(path,{L"*.TXT"},1,true,S_OK,S_OK));
    RUN("strict_star_match_all",filter_case(path,{L"*"},1,true,S_OK,S_OK));
    RUN("strict_star_dot_star_match_all",filter_case(path,{L"*.*"},1,true,S_OK,S_OK));
    RUN("strict_selected_filter_index",filter_case(path,{L"*.md",L"*.txt"},2,true,S_OK,S_OK));
    RUN("strict_zero_index_rejected",filter_case(path,{L"*.txt"},0,true,S_OK,E_INVALIDARG));
    RUN("filter_patterns_are_owned_copies",filter_case(path,{L"*.txt"},1,true,S_OK,S_OK,true));
    RUN("strict_missing_filters_rejected",filter_case(path,{},1,true,S_OK,E_INVALIDARG));
    RUN("complex_filter_explicitly_unsupported",filter_case(path,{L"*.txt;file?.md"},1,true,HRESULT_FROM_WIN32(ERROR_NOT_SUPPORTED),HRESULT_FROM_WIN32(ERROR_NOT_SUPPORTED)));
    RUN("empty_filter_token_rejected",filter_case(path,{L"*.txt;;*.md"},1,true,E_INVALIDARG,E_INVALIDARG));
    RUN("nonstrict_supported_mismatch_allowed",filter_case(path,{L"*.md"},1,false,S_OK,S_OK));
    RUN("null_and_empty_filters_rejected",null_filter_arguments(path));
    RUN("grant_record_copied_positive_success",revocable_grant_case(path,0));
    RUN("delayed_Show_after_revocation_cancelled",revocable_grant_case(path,1));
    RUN("absolute_grant_deadline_cancelled",revocable_grant_case(path,2));
    RUN("grant_generation_mismatch_cancelled",revocable_grant_case(path,3));
    RUN("callback_revocation_denies_GetResults",revocable_grant_case(path,4));
    RUN("revocation_after_callback_read_prevents_acceptance",revocable_grant_case(path,5));
    RUN("malformed_positive_grant_rejected",malformed_grant_rejected(path));
    RUN("cancel_only_ignores_positive_grant",cancel_ignores_positive_grant(path));
#undef RUN
    monitor.stop=true;if(watcher){WaitForSingleObject(watcher,3000);CloseHandle(watcher);}bool deleted=DeleteFileW(path.c_str())!=0;
    CoUninitialize();bool all=passed==total && failed_checks==0 && watcher && deleted && monitor.visible==0 && monitor.foreground_changes==0;
    std::printf("{\"passed\":%s,\"cases\":%d,\"passed_cases\":%d,\"visible_window_samples\":%u,\"foreground_changes\":%u,\"observations\":%u,\"fixture_deleted\":%s,\"limit\":\"1ms sampling cannot exclude shorter transients; real Show is intercepted by test recorder\"}\n",all?"true":"false",total,passed,monitor.visible.load(),monitor.foreground_changes.load(),monitor.samples.load(),deleted?"true":"false");
    return all?0:1;
}
