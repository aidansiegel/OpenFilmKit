// Icons from Tabler Icons 3.52.0 (MIT, https://tabler.io/icons; license in src/icons/LICENSE).
// Every icon in the app comes from here, so they share one outline style. Use icon('name').
'use strict';
(function () {
  const PATHS = {
  "alert-circle": "<path d=\"M3 12a9 9 0 1 0 18 0a9 9 0 0 0 -18 0\" /> <path d=\"M12 8v4\" /> <path d=\"M12 16h.01\" />",
  "alert-triangle": "<path d=\"M12 9v4\" /> <path d=\"M10.363 3.591l-8.106 13.534a1.914 1.914 0 0 0 1.636 2.871h16.214a1.914 1.914 0 0 0 1.636 -2.87l-8.106 -13.536a1.914 1.914 0 0 0 -3.274 0\" /> <path d=\"M12 16h.01\" />",
  "arrow-back-up-double": "<path d=\"M13 14l-4 -4l4 -4\" /> <path d=\"M8 14l-4 -4l4 -4\" /> <path d=\"M9 10h7a4 4 0 1 1 0 8h-1\" />",
  "arrow-back-up": "<path d=\"M9 14l-4 -4l4 -4\" /> <path d=\"M5 10h11a4 4 0 1 1 0 8h-1\" />",
  "arrow-forward-up": "<path d=\"M15 14l4 -4l-4 -4\" /> <path d=\"M19 10h-11a4 4 0 1 0 0 8h1\" />",
  "check": "<path d=\"M5 12l5 5l10 -10\" />",
  "crop": "<path d=\"M8 5v10a1 1 0 0 0 1 1h10\" /> <path d=\"M5 8h10a1 1 0 0 1 1 1v10\" />",
  "eye-off": "<path d=\"M10.585 10.587a2 2 0 0 0 2.829 2.828\" /> <path d=\"M16.681 16.673a8.717 8.717 0 0 1 -4.681 1.327c-3.6 0 -6.6 -2 -9 -6c1.272 -2.12 2.712 -3.678 4.32 -4.674m2.86 -1.146a9.055 9.055 0 0 1 1.82 -.18c3.6 0 6.6 2 9 6c-.666 1.11 -1.379 2.067 -2.138 2.87\" /> <path d=\"M3 3l18 18\" />",
  "flip-horizontal": "<path d=\"M12 3l0 18\" /> <path d=\"M16 7l0 10l5 0l-5 -10\" /> <path d=\"M8 7l0 10l-5 0l5 -10\" />",
  "folder-open": "<path d=\"M5 19l2.757 -7.351a1 1 0 0 1 .936 -.649h12.307a1 1 0 0 1 .986 1.164l-.996 5.211a2 2 0 0 1 -1.964 1.625h-14.026a2 2 0 0 1 -2 -2v-11a2 2 0 0 1 2 -2h4l3 3h7a2 2 0 0 1 2 2v2\" />",
  "info-circle": "<path d=\"M3 12a9 9 0 1 0 18 0a9 9 0 0 0 -18 0\" /> <path d=\"M12 9h.01\" /> <path d=\"M11 12h1v4h1\" />",
  "photo-scan": "<path d=\"M15 8h.01\" /> <path d=\"M6 13l2.644 -2.644a1.21 1.21 0 0 1 1.712 0l3.644 3.644\" /> <path d=\"M13 13l1.644 -1.644a1.21 1.21 0 0 1 1.712 0l1.644 1.644\" /> <path d=\"M3 7v-2a2 2 0 0 1 2 -2h2\" /> <path d=\"M3 17v2a2 2 0 0 0 2 2h2\" /> <path d=\"M17 3h2a2 2 0 0 1 2 2v2\" /> <path d=\"M17 21h2a2 2 0 0 0 2 -2v-2\" />",
  "player-stop": "<path d=\"M5 7a2 2 0 0 1 2 -2h10a2 2 0 0 1 2 2v10a2 2 0 0 1 -2 2h-10a2 2 0 0 1 -2 -2l0 -10\" />",
  "refresh": "<path d=\"M20 11a8.1 8.1 0 0 0 -15.5 -2m-.5 -4v4h4\" /> <path d=\"M4 13a8.1 8.1 0 0 0 15.5 2m.5 4v-4h-4\" />",
  "rotate-clockwise": "<path d=\"M4.05 11a8 8 0 1 1 .5 4m-.5 5v-5h5\" />",
  "rotate": "<path d=\"M19.95 11a8 8 0 1 0 -.5 4m.5 5v-5h-5\" />",
  "x": "<path d=\"M18 6l-12 12\" /> <path d=\"M6 6l12 12\" />",
  "zoom-in": "<path d=\"M3 10a7 7 0 1 0 14 0a7 7 0 1 0 -14 0\" /> <path d=\"M7 10l6 0\" /> <path d=\"M10 7l0 6\" /> <path d=\"M21 21l-6 -6\" />",
  "zoom-out": "<path d=\"M3 10a7 7 0 1 0 14 0a7 7 0 1 0 -14 0\" /> <path d=\"M7 10l6 0\" /> <path d=\"M21 21l-6 -6\" />"
};
  window.icon = function icon(name, size = 16, label = '') {
    const t = document.createElement('template');
    t.innerHTML = `<svg class="ic ic-${name}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" ${label ? `role="img" aria-label="${label}"` : 'aria-hidden="true" focusable="false"'}>${PATHS[name] || ''}</svg>`;
    return t.content.firstChild;
  };
})();
