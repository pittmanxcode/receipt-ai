// Reads Apple Notes modified within the last N hours and prints JSON.
// Run standalone:  osascript -l JavaScript applenotes_probe.js 24
function run(argv) {
  var hours = parseFloat(argv[0]) || 24;
  var since = new Date(Date.now() - hours * 3600 * 1000);
  var Notes = Application('Notes');
  var out = [];

  var candidates;
  try {
    // Filtering in the app is far faster than pulling every note across.
    candidates = Notes.notes.whose({ modificationDate: { '>': since } })();
  } catch (e) {
    candidates = Notes.notes();
  }

  for (var i = 0; i < candidates.length; i++) {
    var n = candidates[i];
    try {
      var mod = n.modificationDate();
      if (mod < since) continue;

      var folder = '';
      try { folder = String(n.container().name()); } catch (e) {}
      // Deleted notes are not capture.
      if (folder === 'Recently Deleted') continue;

      var body = '';
      // plaintext is newer than body; fall back so older macOS still works.
      try { body = n.plaintext(); } catch (e) {
        try { body = n.body(); } catch (e2) { body = ''; }
      }

      out.push({
        id: String(n.id()),
        name: String(n.name() || ''),
        text: String(body || ''),
        folder: folder,
        modified: mod.toISOString()
      });
    } catch (e) {
      // A locked or unreadable note must not stop the whole sweep.
    }
  }
  return JSON.stringify(out);
}
