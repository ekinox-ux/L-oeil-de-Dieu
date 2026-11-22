(function () {
    "use strict";

    var defaultApiBase = "http://127.0.0.1:8500";
    window.APP_CONFIG = Object.assign(
        {
            API_BASE: defaultApiBase,
        },
        window.APP_CONFIG || {}
    );
})();
