"""`openfilmkit ui`: rolls and their frames, a recipe in plain words, a preview, the queue, and the
clean command one key away.

The preview is drawn in the window by textual-image, as well as the terminal allows: real pixels in
Ghostty, Kitty or WezTerm (and Sixel terminals), colored blocks in Terminal.app or over a plain
SSH session. Space opens the full size in Quick Look (macOS) or the default viewer; z zooms in to
see the real pixels, and a click on the preview zooms in there (or, after s, marks a missed spot).
Work is done by the openfilmkit command itself, in the background and one command at a time, with
its output in the log.
"""
import subprocess
import sys
import time
from collections import OrderedDict, deque
from pathlib import Path

import textual_image.renderable  # asks the terminal what it can draw, before Textual starts
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (Button, Checkbox, DataTable, Footer, Header, Label, ListItem, ListView, RadioButton,
                             RadioSet, RichLog, Static)
from textual_image.renderable.halfcell import Image as Halfcell
from textual_image.renderable.tgp import Image as TGP
from textual_image.renderable.tgp import _send_tgp_message
from textual_image.renderable.unicode import Image as Unicode
from textual_image.widget import Image

from .. import __version__
from . import model

QUEUE_EVERY = 30   # seconds between looks at the queue
VIEWS = ('after', 'before', 'side by side', 'fixes')   # b flips after/before; B, ctrl+shift+b side by side
SETTLE = 0.15      # seconds a preview waits for the keys to stop, so a flurry of presses loads one
KEEP = 12          # previews kept, so going back to one is instant
MOST_ZOOM = 32     # z doubles the zoom up to this
BLOCKY = textual_image.renderable.Image in (Halfcell, Unicode)   # no pixels here: colored blocks only
SHIFT_CLICKS_TO_US = '\x1b[>1s'          # XTSHIFTESCAPE 1: shift-clicks are reported, not kept
SHIFT_CLICKS_TO_TERMINAL = '\x1b[>0s'    # and 0: the terminal's again (to extend a selection)


def configured():
    """({hosts}, {queues}) from hosts.toml, for the clean dialog and the queue panel."""
    try:
        from ..queues import _config
        cfg = _config()
    except Exception:   # an unreadable hosts.toml shouldn't keep the UI from starting
        return {}, {}
    return cfg.get('hosts', {}), cfg.get('queues', {})


class CleanDialog(ModalScreen):
    """Options and where to do the work; returns an openfilmkit clean argument list, or None."""
    BINDINGS = [Binding('escape', 'cancel', 'Cancel')]

    def __init__(self, count, hosts, queues, fixed=None):
        """`fixed`: (arguments, why) when the options can't be chosen: with spots marked, the new
        copy is made with the same options as the one they were marked on."""
        super().__init__()
        self.count, self.hosts, self.queues, self.fixed = count, hosts, queues, fixed

    def compose(self) -> ComposeResult:
        with Vertical(id='dialog'):
            yield Label(f"Clean {self.count} photo{'s' if self.count != 1 else ''}", id='dialog-title')
            if self.fixed:
                yield Label(self.fixed[1], id='fixed')
            else:
                yield Checkbox('Scratches and debris too (filled in by a model)', id='scratches')
                yield Checkbox('Correct levels and color cast', id='color')
                yield Checkbox('Darken red eyes', id='red-eye')
                yield Checkbox('Keep film-holder edges (no crop)', id='no-crop')
                yield Checkbox('Spare long straight lines (with scratches)', id='spare-long-lines')
            yield Label('Where')
            with RadioSet(id='where'):
                yield RadioButton('Here', value=True, id='here')
                for name in self.hosts:
                    yield RadioButton(f'On {name}', id=f'on-{name}')
                for name in self.queues:
                    yield RadioButton(f'Into the {name} queue', id=f'queue-{name}')
            with Horizontal(id='dialog-buttons'):
                yield Button('Clean', variant='primary', id='go')
                yield Button('Cancel', id='cancel')

    @on(Button.Pressed, '#go')
    def go(self):
        args = list(self.fixed[0]) if self.fixed else [f'--{box.id}' for box in self.query(Checkbox) if box.value]
        chosen = self.query_one('#where', RadioSet).pressed_button
        if chosen and chosen.id.startswith('on-'):
            args += ['--on', chosen.id[3:]]
        elif chosen and chosen.id.startswith('queue-'):
            args += ['--queue', chosen.id[6:]]
        self.dismiss(args)

    @on(Button.Pressed, '#cancel')
    def action_cancel(self):
        self.dismiss(None)


class ConvertDialog(ModalScreen):
    """What kind of copy to convert the scan masters to; returns openfilmkit convert arguments, or None."""
    BINDINGS = [Binding('escape', 'cancel', 'Cancel')]

    def __init__(self, count):
        super().__init__()
        self.count = count

    def compose(self) -> ComposeResult:
        with Vertical(id='dialog'):
            yield Label(f"Convert {self.count} frame{'s' if self.count != 1 else ''}", id='dialog-title')
            yield Label('From the roll\'s film choice, as a starting conversion to clean and adjust.')
            with RadioSet(id='kind'):
                yield RadioButton('JPEG viewing copy (1800 px on the long side)', value=True, id='jpeg')
                yield RadioButton('16-bit TIFF at full resolution', id='tiff')
            with Horizontal(id='dialog-buttons'):
                yield Button('Convert', variant='primary', id='go')
                yield Button('Cancel', id='cancel')

    @on(Button.Pressed, '#go')
    def go(self):
        chosen = self.query_one('#kind', RadioSet).pressed_button
        self.dismiss(['--tiff'] if chosen and chosen.id == 'tiff' else [])

    @on(Button.Pressed, '#cancel')
    def action_cancel(self):
        self.dismiss(None)


class InPlaceTGP(TGP):
    """A picture in Ghostty, Kitty or WezTerm (the Terminal Graphics Protocol), sent under the preview's
    one ID, so each new picture replaces the last in the terminal (the protocol deletes an image when
    data comes for its ID again). textual-image (0.14.3) sends each under a new ID and deletes the old
    with a=d,I=<id>, which takes pictures off the screen but keeps them all, so a Ghostty tab grew by
    gigabytes in an evening. Freeing each one (d=I) isn't enough in Ghostty 1.3.1, which keeps a freed
    picture unless a frame with no picture on screen follows. q=2 keeps any reply out of what Textual
    reads as keys."""
    terminal_id = None   # set by the preview

    def _send_image_to_terminal(self, width, height):
        self.terminal_image_id = self.terminal_id or next(TGP._image_id_counter)
        data = self._image_data.scaled(width, height).to_base64()
        while data:
            chunk, data = data[:4096], data[4096:]
            _send_tgp_message(i=self.terminal_image_id, m=1 if data else 0, f=100, payload=chunk, q=2)

    def cleanup(self):
        """Take it off the screen (d=i: its placement), keeping the picture for the next to replace."""
        if self.terminal_image_id:
            _send_tgp_message(a='d', d='i', i=self.terminal_image_id, q=2)
            self.terminal_image_id = None


class Preview(Image, Renderable=InPlaceTGP if Image._Renderable is TGP else Image._Renderable):
    """The preview, telling the app where on the picture it was clicked."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.terminal_id = next(TGP._image_id_counter)

    def render(self):
        picture = super().render()
        if isinstance(picture, InPlaceTGP):
            picture.terminal_id = self.terminal_id
        return picture

    def on_click(self, event: events.Click):
        offset = event.get_content_offset(self)
        size = self.content_size
        if offset is not None and size.width and size.height:
            self.app.preview_clicked((offset.x + 0.5) / size.width, (offset.y + 0.5) / size.height,
                                     shift=event.shift)

    def on_unmount(self):
        if issubclass(self._Renderable, InPlaceTGP):   # freed, so quitting leaves nothing in the terminal
            _send_tgp_message(a='d', d='I', i=self.terminal_id, q=2)


class HelpScreen(ModalScreen):
    BINDINGS = [Binding('escape,q,question_mark', 'app.pop_screen', 'Close')]
    KEYS = [('↑ ↓', 'move through the frames (← → between the list of rolls and the frames)'),
            ('m', 'mark or unmark a frame; a marks every frame that needs cleaning'),
            ('v', 'convert the marked frames (or the one selected) from their scan masters'),
            ('c', 'clean the marked frames (or the one selected): here, on a host or into a queue'),
            ('space', 'open the full size in Quick Look or the default viewer (with its original)'),
            ('b', 'flip the preview between after (the cleaned copy) and before (the original), in place'),
            ('B', 'before and after side by side (again: back to after)'),
            ('^⇧B', 'side by side with every pixel changed by more than 8 levels in red (ctrl+b too)'),
            ('z  Z', 'zoom in and out (a click on the preview zooms in there, a shift-click out; '
                     'escape: all of it)'),
            ('hjkl', 'move around a zoomed picture (shift+arrows too)'),
            ('s', 'mark missed spots: click them on the after picture (again: unmark); [ ] resize the last'),
            ('C', 'collect what the queue has finished'),
            ('r', 'look again at the files and the queue'),
            ('q', 'quit')]

    def compose(self) -> ComposeResult:
        with Vertical(id='dialog'):
            yield Label('OpenFilmKit keys', id='dialog-title')
            for key, what in self.KEYS:
                yield Label(f'[b]{key:>6}[/b]  {what}')
            yield Label('\nPreviews are sharpest in Ghostty, Kitty or WezTerm.'
                        + ('\nThis terminal shows colored blocks: a low-resolution guide.' if BLOCKY else ''))


class OpenFilmKitApp(App):
    TITLE = 'OpenFilmKit'
    CSS = '''
    #places { width: 24; border-right: solid $primary-darken-2; }
    #title { padding: 0 1; color: $text-muted; height: 1; }
    #frames { height: auto; max-height: 45%; }
    #detail { height: 1fr; min-height: 12; }
    #recipe { width: 1fr; padding: 0 1; overflow-y: auto; }
    #preview-side { width: 1fr; height: 100%; }
    #preview-caption { height: 1; color: $text-muted; text-align: center; }
    #preview-box { height: 1fr; align: center middle; }
    #preview { width: auto; height: auto; }   /* as big as fits, keeping the photo's shape */
    #queue { height: auto; max-height: 8; border-top: solid $primary-darken-2; padding: 0 1; }
    #log { height: 6; border-top: solid $primary-darken-2; display: none; }
    CleanDialog, ConvertDialog, HelpScreen { align: center middle; }
    #fixed { margin-bottom: 1; }
    #dialog > Label { width: 100%; }   /* wrapped, not cut off */
    #dialog { width: 72; height: auto; padding: 1 2; border: thick $primary; background: $surface; }
    #dialog-title { text-style: bold; margin-bottom: 1; }
    #dialog-buttons { height: auto; margin-top: 1; }
    '''
    BINDINGS = [Binding('space', 'look', 'Quick Look'), Binding('b', 'show(\'before\')', 'Before/after'),
                Binding('B', 'show(\'side by side\')', 'Side by side'),
                Binding('ctrl+shift+b,ctrl+b', 'show(\'fixes\')', 'Fixes', key_display='^⇧B'),
                Binding('z,plus,equals_sign', 'zoom(2)', 'Zoom'), Binding('Z,minus', 'zoom(0.5)', 'Zoom out', show=False),
                Binding('h,shift+left', 'pan(-1, 0)', show=False), Binding('l,shift+right', 'pan(1, 0)', show=False),
                Binding('k,shift+up', 'pan(0, -1)', show=False), Binding('j,shift+down', 'pan(0, 1)', show=False),
                Binding('escape', 'back', show=False),
                Binding('s', 'spotting', 'Spots'), Binding('right_square_bracket', 'resize_spot(1.5)', show=False),
                Binding('left_square_bracket', 'resize_spot(0.667)', show=False),
                Binding('m', 'mark', 'Mark'), Binding('a', 'mark_needing', 'Mark to clean', show=False),
                Binding('v', 'convert', 'Convert…'), Binding('c', 'clean', 'Clean…'),
                Binding('C', 'collect', 'Collect', show=False),
                Binding('r', 'refresh', 'Refresh'), Binding('question_mark', 'help', 'Help'),
                Binding('q', 'quit', 'Quit')]

    def __init__(self, folder='.'):
        super().__init__()
        self.folder = Path(folder).expanduser().resolve()
        self.places = model.places(self.folder)
        self.place = self.places[0][1] if self.places else self.folder
        self.items, self.marked, self.jobs = [], set(), {}
        self.view = 'after'
        self.zoom, self.center = 1, (0.5, 0.5)   # center: fractions across and down the picture
        self.spotting = False            # clicks on the preview mark spots instead of zooming
        self.spots = {}                  # str(cleaned copy) -> [[x, y, r]] in its pixels
        self.shown = None                # the item whose preview is showing, and its panels
        self.panels = []
        self.showing = None              # everything the preview shows, so a refresh that changes none of it keeps it
        self.wanted = 0                  # the newest preview asked for; older loads are dropped
        self.previews = OrderedDict()    # (paths, view, region, mtimes) -> (image, caption)
        self.told_blocky = False
        self.took_shift_clicks = False   # whether the terminal was asked to report shift-clicks
        self.running = {}                # key(item) -> what's being done to it
        self.pending = deque()           # commands waiting their turn: (argv, about, doing)
        self.runner = False              # whether run_commands is working through them
        self.hosts, self.queues = configured()
        self.sub_title = __version__

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield ListView(*[ListItem(Label(label)) for label, _ in self.places], id='places')
            with Vertical():
                yield Static(id='title')
                yield DataTable(id='frames', cursor_type='row', zebra_stripes=True)
                with Horizontal(id='detail'):
                    yield Static(id='recipe')
                    with Vertical(id='preview-side'):
                        yield Static(id='preview-caption')
                        with Container(id='preview-box'):
                            yield Preview(id='preview')
        yield Static(id='queue')
        yield RichLog(id='log', wrap=True, markup=True)
        yield Footer()

    def on_mount(self):
        driver = getattr(self, '_driver', None)        # Textual has no public way to write to it
        if driver is not None and not self.is_headless:
            # XTSHIFTESCAPE: terminals keep a shift-click for extending a selection (Ghostty's
            # mouse-shift-capture, xterm's own) unless the program asks for it. run() hands it back.
            driver.write(SHIFT_CLICKS_TO_US)
            self.took_shift_clicks = True
        table = self.query_one('#frames', DataTable)
        table.add_columns(' ', 'frame', 'master', 'conversion', 'cleaned', 'state')
        self.query_one('#queue').display = bool(self.queues)
        self.show_place()
        table.focus()
        if self.queues:
            self.look_at_queue()
            self.set_interval(QUEUE_EVERY, self.look_at_queue)

    # -- what's shown
    @staticmethod
    def key(item):
        """What a frame is known by while it's marked or worked on: its conversion, or its master."""
        return item.source or item.master

    def show_place(self, force=False):
        self.items = model.items(self.place)
        self.marked &= {self.key(i) for i in self.items}
        self.query_one('#title', Static).update(
            f'{self.place.name} · {model.roll_title(self.place)} · {len(self.items)} photos')
        self.fill_table(force)

    def state_of(self, item):
        if self.key(item) in self.running:
            return self.running[self.key(item)]
        job = self.jobs.get(str(item.source)) if item.source else None
        if job:
            where = f" on {job['worker']}" if job.get('worker') else ''
            return {'done': 'done → collect (C)'}.get(job['state'], job['state'] + where)
        spots = len(self.spots.get(str(item.latest), []))
        return item.state + (f" · {spots} spot{'s' if spots != 1 else ''}" if spots else '')

    def fill_table(self, force=False):
        table = self.query_one('#frames', DataTable)
        row = table.cursor_row
        table.clear()
        for i, item in enumerate(self.items):
            table.add_row('✓' if self.key(item) in self.marked else ' ', item.name,
                          '✓' if item.master else '—', item.source.name if item.source else '—',
                          f'v{len(item.cleaned)}' if item.cleaned else '—', self.state_of(item), key=str(i))
        if self.items:
            table.move_cursor(row=min(max(row, 0), len(self.items) - 1))
        self.show_item(force)

    def current(self):
        table = self.query_one('#frames', DataTable)
        if not self.items or table.cursor_row is None or table.cursor_row >= len(self.items):
            return None
        return self.items[table.cursor_row]

    def show_item(self, force=False):
        """Show the selected photo's recipe and preview. The preview is loaded again only when
        something it shows has changed (or with `force`), so the table being filled again, as
        commands start and end and the queue is looked at, doesn't keep restarting it."""
        item = self.current()
        if (self.key(item) if item else None) != self.shown:
            self.shown = self.key(item) if item else None
            self.zoom, self.center = 1, (0.5, 0.5)    # a new photo starts whole
        self.query_one('#recipe', Static).update('\n'.join(model.describe(item)) if item else 'No photos here.')
        showing = (item.source, item.master, item.latest, tuple(map(tuple, self.spots.get(str(item.latest), []))))\
            if item else None
        showing = (showing, self.view, self.region(), self.spotting)
        if showing == self.showing and not force:
            return
        self.showing = showing
        self.wanted += 1
        self.query_one('#preview-caption', Static).update(f'{self.view} …')
        self.load_preview(item, self.view, self.region(), self.wanted)

    def region(self):
        return model.zoomed(self.zoom, self.center)

    def extras(self):
        """What the caption starts with for spot marking and the zoom (first, so a narrow window
        still shows them)."""
        out = 'marking spots · ' if self.spotting else ''
        return out + (f'×{self.zoom:g} · ' if self.zoom > 1 else '')

    def from_thread(self, fn, *args):
        """Hand work back to the UI from a background thread, unless the app has closed meanwhile
        (quitting while a preview loads, say): then there's nothing left to update."""
        def unless_closing():   # asked again at its turn on the UI's thread: closing may have begun while it waited
            if self.is_running:
                fn(*args)
        if not self.is_running:
            return
        try:
            self.call_from_thread(unless_closing)
        except RuntimeError:
            pass

    def make_preview(self, item, view, region=None):
        """(image, caption) for an item in one of the VIEWS, or the `region` of it (see model.zoomed())."""
        if not item or not (item.latest or item.source or item.master):
            return None, ''
        if not item.latest:
            shown = item.source or item.master
            if shown is item.master:
                return None, 'not converted yet (v converts it): no preview of a scan master'
            return model.preview_image(shown, region=region), f'{shown.name} (not cleaned yet)'
        if view == 'after':
            return model.preview_image(item.latest, region=region), f'after: {item.latest.name}   (b: before)'
        if view == 'before':
            return model.before_image(item.source, item.latest, region=region), f'before: {item.source.name}   (b: after)'
        if view == 'side by side':
            return (model.side_by_side(item.source, item.latest, region=region)[0],
                    f'before: {item.source.name}  |  after: {item.latest.name}')
        try:
            picture, n = model.side_by_side(item.source, item.latest, fixes=True, region=region)
        except model.NotComparable as e:
            return model.side_by_side(item.source, item.latest, region=region)[0], f'fixes: can\'t mark them; {e}'
        return picture, f'before | after: {n:,} pixels changed{" here" if region else ""}, in red'

    @work(thread=True, exclusive=True, group='preview')
    def load_preview(self, item, view, region, ticket):
        time.sleep(SETTLE)
        if ticket != self.wanted:
            return                               # another key came since: that one's preview counts
        paths = tuple(str(p) for p in (item.source, item.latest) if p) if item else ()
        key = (paths, view, region, tuple(Path(p).stat().st_mtime_ns for p in paths if Path(p).exists()))
        try:
            found = self.previews.get(key) or self.make_preview(item, view, region)
            picture, caption = found
            spots = self.spots.get(str(item.latest)) if item and item.latest else None
            if picture is not None and spots:
                picture = model.draw_spots(picture, spots, model.size_of(item.latest), region)
        except Exception as e:   # an unreadable file: say so, keep going
            found = picture, caption = None, f'preview: {e}'
        self.previews[key] = found
        self.previews.move_to_end(key)
        while len(self.previews) > KEEP:
            self.previews.popitem(last=False)
        self.from_thread(self.show_preview, ticket, picture, caption)

    def show_preview(self, ticket, picture, caption):
        if ticket != self.wanted:   # checked here, on the UI's thread: a newer key may have come meanwhile
            return
        preview = self.query_one('#preview', Image)
        preview.image = picture
        preview.refresh(layout=True)             # size it for this picture's shape now, not the last one's
        self.panels = picture.info.get('panels', []) if picture is not None else []
        self.query_one('#preview-caption', Static).update(self.extras() + caption)
        if picture is not None and BLOCKY and not self.told_blocky:
            self.told_blocky = True
            self.notify(self.blocky_notice(), title='Low-resolution preview', timeout=15)

    @staticmethod
    def blocky_notice():
        """Said the first time a preview is drawn in colored blocks."""
        if model.screen_here():
            viewer = 'Quick Look' if sys.platform == 'darwin' else 'the default viewer'
            return (f"This terminal can't show images, so the preview is a low-resolution guide made of "
                    f"colored blocks. Space opens the full size in {viewer}; Ghostty, Kitty or WezTerm "
                    'show sharp previews here.')
        return ("This terminal can't show images, and there's no screen here to open an image viewer on, "
                'so the preview is a low-resolution guide made of colored blocks. z zooms in for a closer look.')

    # An event from the table or the list can still be waiting when the app closes and the screen is
    # taken down, as Textual's test pilot does it: the widgets these look for are gone by then.
    @on(DataTable.RowHighlighted, '#frames')
    def row_moved(self):
        if self.is_running:
            self.show_item()

    @on(ListView.Highlighted, '#places')
    def place_moved(self, event):
        if not self.is_running:
            return
        index = self.query_one('#places', ListView).index
        if index is not None and self.places[index][1] != self.place:
            self.place = self.places[index][1]
            self.show_place()

    def log_line(self, text):
        log = self.query_one('#log', RichLog)
        log.display = True   # shown once there's something to show
        log.write(text)

    # -- keys
    def action_look(self):
        item = self.current()
        if not item:
            return
        try:
            how = model.open_in_viewer(item.source, item.latest) if item.latest else \
                model.open_in_viewer(item.source or item.master)
            self.notify(f'Opened in {how}')
        except OSError as e:
            self.notify(str(e), severity='warning')

    def preview_clicked(self, fx, fy, shift=False):
        """A click at (fx, fy), fractions across and down the preview: zoom in there, or, while
        marking spots, mark or unmark one on the after picture. A shift-click zooms out there,
        marking spots or not."""
        item = self.current()
        for n, (x0, x1, y0, y1) in enumerate(self.panels):
            if x0 <= fx <= x1 and y0 <= fy <= y1:
                break
        else:
            return                                # the gap between before and after
        l, t, r, b = self.region() or (0, 0, 1, 1)
        px = l + (fx - x0) / (x1 - x0) * (r - l)  # where on the whole picture
        py = t + (fy - y0) / (y1 - y0) * (b - t)
        if shift or not self.spotting:
            self.center = (px, py)
            self.action_zoom(0.5 if shift else 2)
            return
        after = {'after': 0, 'side by side': 1, 'fixes': 1}.get(self.view)
        if not (item and item.latest) or after != n:
            self.notify('Spots are marked on the after picture of a cleaned copy', severity='warning')
            return
        size = model.size_of(item.latest)
        x, y = int(px * size[0]), int(py * size[1])
        spots = self.spots.setdefault(str(item.latest), [])
        hit = [s for s in spots if (s[0] - x) ** 2 + (s[1] - y) ** 2 <= s[2] ** 2]
        if hit:
            spots.remove(hit[-1])
        else:
            spots.append([x, y, model.default_radius(size)])
        if not spots:
            del self.spots[str(item.latest)]
        self.fill_table()

    def action_zoom(self, factor):
        self.zoom = min(max(self.zoom * factor, 1), MOST_ZOOM)
        self.reframe()

    def action_pan(self, dx, dy):
        if self.zoom <= 1:
            return
        step = 0.25 / self.zoom                   # a quarter of what's shown
        self.center = (self.center[0] + dx * step, self.center[1] + dy * step)
        self.reframe()

    def reframe(self):
        """Show the part of the picture the zoom and center say, the center kept where the part
        stays inside the picture (so panning past an edge doesn't build up)."""
        region = self.region()
        self.center = ((region[0] + region[2]) / 2, (region[1] + region[3]) / 2) if region else (0.5, 0.5)
        self.show_item()

    def action_back(self):
        if self.spotting:
            self.action_spotting()
        elif self.zoom > 1:
            self.zoom, self.center = 1, (0.5, 0.5)
            self.show_item()

    def action_spotting(self):
        self.spotting = not self.spotting
        if self.spotting:
            self.notify('Click missed spots on the after picture (zoom in first for precision); '
                        'click a ring again to unmark it, [ and ] resize the last one, c cleans with them.')
        self.show_item()

    def action_resize_spot(self, factor):
        item = self.current()
        spots = self.spots.get(str(item.latest)) if item and item.latest else None
        if not spots:
            return
        size = model.size_of(item.latest)
        spots[-1][2] = min(max(1, round(spots[-1][2] * factor)), min(size) // 10)
        self.show_item()

    def action_show(self, view):
        """b flips between after and before; B and ctrl+shift+b turn their side-by-side view on, or
        back off to after."""
        if view == 'before':
            self.view = 'after' if self.view == 'before' else 'before'
        else:
            self.view = 'after' if self.view == view else view
        self.show_item()

    def action_mark(self):
        item = self.current()
        if item and self.key(item):
            self.marked ^= {self.key(item)}
            self.fill_table()

    def action_mark_needing(self):
        self.marked = {i.source for i in self.items if i.source and not i.cleaned}
        self.fill_table()

    def chosen(self, can):
        """The marked items `can` work on, or the selected one; None (having said why) if none."""
        marked = [i for i in self.items if self.key(i) in self.marked]
        found = [i for i in marked or [self.current()] if i and can(i)]
        return found or None

    def action_refresh(self, force=True):
        self.show_place(force)
        if self.queues:
            self.look_at_queue()

    def action_help(self):
        self.push_screen(HelpScreen())

    def action_clean(self):
        chosen = self.chosen(lambda i: i.source)
        if not chosen:
            self.notify('Nothing to clean: convert the frame first (v)', severity='warning')
            return
        fixed, spotted = None, [i for i in chosen if self.spots.get(str(i.latest))]
        if spotted:
            if len(chosen) > 1:
                self.notify('Spots are cleaned one photo at a time: unmark the other frames first',
                            severity='warning')
                return
            item = spotted[0]
            options = (model.read_recipe(item.latest) or {}).get('options')
            if options is None:
                self.notify(f"Can't tell how {item.latest.name} was cleaned (no recipe), so its spots "
                            "can't be placed", severity='warning')
                return
            spots = self.spots[str(item.latest)]
            fixed = (model.clean_args(options) + [a for x, y, r in spots for a in ('--spot', f'{x},{y},{r}')],
                     f"With the same options as {item.latest.name}, so the {len(spots)} "
                     f"spot{'s' if len(spots) != 1 else ''} you marked land in the same places.")

        def start(args):
            if args is not None:   # the spots stay with the old copy: shown again if the clean fails
                where = f" on {args[args.index('--on') + 1]}" if '--on' in args else \
                    ' into the queue' if '--queue' in args else ''
                self.start_command(['clean', *(str(i.source) for i in chosen), *args],
                                   [self.key(i) for i in chosen], f'cleaning{where}…')
                self.marked.clear()
                self.fill_table()
        self.push_screen(CleanDialog(len(chosen), self.hosts, self.queues, fixed), start)

    def action_convert(self):
        chosen = self.chosen(lambda i: i.master)
        if not chosen:
            self.notify('No scan master to convert here', severity='warning')
            return
        chosen = list({i.master: i for i in chosen}.values())   # a frame converted twice is one master

        def start(args):
            if args is not None:
                for item in chosen:
                    self.start_command(['convert', str(item.master), *args], [self.key(item)], 'converting…')
                self.marked.clear()
                self.fill_table()
        self.push_screen(ConvertDialog(len(chosen)), start)

    def action_collect(self):
        if not self.queues:
            self.notify('No queue in hosts.toml', severity='warning')
            return
        for name in self.queues:
            self.start_command(['queue', 'collect', name], [], 'collecting…')

    # -- background work
    def start_command(self, argv, about, doing):
        """Run an openfilmkit command after any before it, showing `doing` against the frames
        it's `about` meanwhile."""
        for k in about:
            self.running[k] = 'waiting'
        self.pending.append((argv, about, doing))
        if not self.runner:
            self.runner = True
            self.run_commands()
        else:
            self.log_line(f'[dim]waiting to run: openfilmkit {" ".join(argv)}[/dim]')
        self.fill_table()

    def next_command(self):
        """The next command to run (on the UI's thread, so start_command can't slip one in as the
        runner stops), or None: then the runner stops."""
        if self.pending and self.is_running:
            return self.pending.popleft()
        self.runner = False
        return None

    def _doing(self, about, doing):
        for k in about:
            if doing:
                self.running[k] = doing
            else:
                self.running.pop(k, None)
        self.fill_table()

    @work(thread=True, group='commands')
    def run_commands(self):
        """Run the waiting commands one after another, in one thread however many wait (they'd
        otherwise each hold one of the threads previews load in). Quitting runs no more of them."""
        while True:
            try:
                command = self.call_from_thread(self.next_command) if self.is_running else None
            except RuntimeError:                  # the app closed meanwhile
                command = None
            if command is None:
                return
            self.run_command(*command)

    def run_command(self, argv, about, doing):
        """Run an openfilmkit command (as a separate process, so its memory goes when it ends; one at
        a time, since cleaning a big scan takes most of a small machine's memory), showing what it says."""
        self.from_thread(self._doing, about, doing)
        self.from_thread(self.log_line, f'[b]openfilmkit {" ".join(argv)}[/b]')
        try:
            proc = subprocess.Popen([sys.executable, '-m', 'openfilmkit', *argv], stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in proc.stdout:
                self.from_thread(self.log_line, line.rstrip())
            proc.wait()
            self.from_thread(self.log_line, '[green]done[/green]' if proc.returncode == 0
                             else f'[red]ended with status {proc.returncode}[/red]')
        finally:
            self.from_thread(self._doing, about, None)
            self.from_thread(self.action_refresh, False)   # new files have new names: shown if they're this photo's

    @work(thread=True, exclusive=True, group='queue')
    def look_at_queue(self):
        from .. import queues
        jobs, lines = {}, []
        for name in self.queues:
            try:
                info = queues.status(queues.queue_settings(name))
            except Exception as e:
                lines.append(f'Queue {name}: [red]{e}[/red]')
                continue
            for row in info['jobs']:
                jobs[row.get('photo')] = row
            counts = {}
            for row in info['jobs']:
                counts[row['state']] = counts.get(row['state'], 0) + 1
            summary = ', '.join(f'{n} {s}' for s, n in counts.items()) or 'empty'
            lines.append(f'Queue [b]{name}[/b]: {summary}')
            for w in info['workers']:
                busy = f"running {w['running'][:12]}" if w.get('running') else 'idle'
                seen = f"not seen for {queues.ago(w['seen_ago_s'])}" if w['silent'] else f"seen {queues.ago(w['seen_ago_s'])} ago"
                lines.append(f"  worker {w['name']}: {busy}, up to {w.get('memory_gb')} GB, {seen}")
        self.from_thread(self._queue_seen, jobs, '\n'.join(lines))

    def _queue_seen(self, jobs, text):
        self.jobs = jobs
        self.query_one('#queue', Static).update(text)
        self.fill_table()


def run(folder='.'):
    app = OpenFilmKitApp(folder)
    try:
        app.run()
    finally:
        if app.took_shift_clicks and sys.__stdout__:   # the terminal's own use of shift-click, back
            sys.__stdout__.write(SHIFT_CLICKS_TO_TERMINAL)
            sys.__stdout__.flush()
