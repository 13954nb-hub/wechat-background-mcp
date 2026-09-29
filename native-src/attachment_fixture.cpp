#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shobjidl.h>
#include <wchar.h>
using CreateFn=HRESULT(WINAPI *)(REFCLSID,LPUNKNOWN,DWORD,REFIID,LPVOID *);
static CreateFn slot=nullptr;
class ResultSink final : public IFileDialogEvents {
    LONG refs=1;
public:
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid,void **out) override {
        if(!out)return E_POINTER;*out=nullptr;
        if(iid!=IID_IUnknown && iid!=IID_IFileDialogEvents)return E_NOINTERFACE;
        *out=static_cast<IFileDialogEvents *>(this);AddRef();return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override{return InterlockedIncrement(&refs);}
    ULONG STDMETHODCALLTYPE Release() override{LONG n=InterlockedDecrement(&refs);if(!n)delete this;return n;}
    HRESULT STDMETHODCALLTYPE OnFileOk(IFileDialog *dialog) override {
        IFileOpenDialog *open=nullptr;HRESULT hr=dialog->QueryInterface(IID_IFileOpenDialog,reinterpret_cast<void **>(&open));
        if(FAILED(hr))return hr;
        IShellItemArray *array=nullptr;hr=open->GetResults(&array);open->Release();
        if(FAILED(hr))return hr;
        DWORD count=0;hr=array->GetCount(&count);array->Release();
        return SUCCEEDED(hr) && count==1?S_OK:E_FAIL;
    }
    HRESULT STDMETHODCALLTYPE OnFolderChanging(IFileDialog *,IShellItem *) override{return S_OK;}
    HRESULT STDMETHODCALLTYPE OnFolderChange(IFileDialog *) override{return S_OK;}
    HRESULT STDMETHODCALLTYPE OnSelectionChange(IFileDialog *) override{return S_OK;}
    HRESULT STDMETHODCALLTYPE OnShareViolation(IFileDialog *,IShellItem *,FDE_SHAREVIOLATION_RESPONSE *out) override{if(out)*out=FDESVR_DEFAULT;return S_OK;}
    HRESULT STDMETHODCALLTYPE OnTypeChange(IFileDialog *) override{return S_OK;}
    HRESULT STDMETHODCALLTYPE OnOverwrite(IFileDialog *,IShellItem *,FDE_OVERWRITE_RESPONSE *out) override{if(out)*out=FDEOR_DEFAULT;return S_OK;}
};
extern "C" __declspec(dllexport) void *volatile *AttachmentFixtureSlot() {
    if(!slot) slot=reinterpret_cast<CreateFn>(GetProcAddress(GetModuleHandleW(L"ole32.dll"),"CoCreateInstance"));
    return reinterpret_cast<void *volatile *>(&slot);
}
extern "C" __declspec(dllexport) HRESULT AttachmentFixtureRun(HWND owner) {
    if(!slot) return E_UNEXPECTED;
    IFileOpenDialog *dialog=nullptr;
    HRESULT hr=slot(CLSID_FileOpenDialog,nullptr,CLSCTX_INPROC_SERVER,IID_IFileOpenDialog,reinterpret_cast<void **>(&dialog));
    if(FAILED(hr))return hr;
    // The mock can never display a native picker even if routing fails.
    void **vtable=*reinterpret_cast<void ***>(dialog);
    HMODULE module=nullptr;wchar_t path[32768]={};
    bool proxy=GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS|GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
        reinterpret_cast<LPCWSTR>(vtable[0]),&module) && GetModuleFileNameW(module,path,32768);
    const wchar_t *leaf=wcsrchr(path,L'\\');leaf=leaf?leaf+1:path;
    if(!proxy || _wcsicmp(leaf,L"wxbg_attachment.dll")) hr=E_ABORT;
    else {
        ResultSink *sink=new ResultSink();DWORD cookie=0;
        hr=dialog->Advise(sink,&cookie);sink->Release();
        if(SUCCEEDED(hr)) {
            COMDLG_FILTERSPEC filter={L"Fixture text",L"*.txt"};
            hr=dialog->SetFileTypes(1,&filter);
            if(SUCCEEDED(hr))hr=dialog->SetOptions(FOS_FORCEFILESYSTEM|FOS_FILEMUSTEXIST|FOS_PATHMUSTEXIST);
            if(SUCCEEDED(hr))hr=dialog->Show(owner);
            dialog->Unadvise(cookie);
        }
    }
    dialog->Release();return hr;
}
