/* Pipe-friendly entry point for the original David Betz XLISP engine. */
#include "xlisp.h"
#ifdef _WIN32
#include <io.h>
#include <fcntl.h>
#endif
int main(int argc, const char *argv[]) {
    xlCallbacks *cb;
#ifdef _WIN32
    _setmode(_fileno(stdin), _O_BINARY);
    _setmode(_fileno(stdout), _O_BINARY);
#endif
    setvbuf(stdout, NULL, _IONBF, 0);
    setvbuf(stderr, NULL, _IONBF, 0);
    /* The launcher sets cwd to engine/. Avoid the legacy 100-byte argv buffer. */
    cb = xlDefaultCallbacks(NULL);
    if (!xlInit(cb, argc, argv, NULL)) return 1;
    xlInfo("%s", xlBanner());
    if (xlLoadFile("xlisp.lsp") != xlsSuccess) return 2;
    xlCallFunctionByName(NULL, 0, "*TOPLEVEL*", 0);
    return 0;
}
