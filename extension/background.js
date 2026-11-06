// corla-observer extension: the toolbar button opens the page listing this observer's links (setup.html).
// It does nothing else: no network access, no access to any web page.
var api = typeof browser !== "undefined" ? browser : chrome;
api.action.onClicked.addListener(function () {
  api.tabs.create({ url: api.runtime.getURL("setup.html") });
});
