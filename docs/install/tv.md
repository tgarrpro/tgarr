# tgarr on a phone's home screen, and on the television

tgarr is a web page. Two small things make it sit on a device like an app.

## On the home screen (phone, tablet)

Open tgarr in the browser and choose **Add to Home Screen** (Safari: the share button;
Chrome: the ⋮ menu). There are two icons to be had:

- from any page of tgarr: **tgarr**, the whole of it;
- from the **Media Center** page: **Media Center** alone - the one for a child's tablet.

On an iPhone or iPad it then opens full screen, without the browser's bars. On Android,
Chrome does that only for an address that begins with `https://`; at a plain
`http://192.168…` address the icon is a shortcut that opens in the browser. tgarr works
the same either way.

## On the television (Android TV, Google TV, Fire TV)

`tgarr-tv.apk`, from the [releases page](https://github.com/tgarrpro/tgarr/releases), is a
frame around the Media Center: full screen, driven by the remote control. It was run on an
Android TV 14 emulator; not yet on a television in a living room - tell us how it does on yours.

- It **looks for tgarr on the home network** by itself (tgarr 0.5.11 or later, on port
  8765). If it does not find it, type the address you open tgarr at in a browser.
- **Arrows** move to the nearest thing in that direction, **OK** opens it, **Back** goes
  back. To search, press OK on **Find**: the keyboard opens. **Menu** offers another address, or loading the page again.
- A television that was not unlocked with the PIN shows the Media Center and nothing else,
  with the time rules you set - that is tgarr's rule for every device, and the reason this
  app shows the Media Center.

Installing it: the app is not in a store. On Android TV / Google TV, send the file to the
television (a USB stick, or an app such as "Send files to TV") and open it with a file
manager; the television asks once to allow installing from that app. On a Fire TV, the
"Downloader" app fetches it from the release's address.

The arrows also work in an ordinary browser, with a keyboard: the Media Center is the same
page everywhere. Add `?tv=1` to its address to have it open with the first item chosen.

The app's source is in [`tv/`](../../tv): one Java file, no libraries, 20 kB.

It is signed by tgarr's own key - certificate `CN=tgarr`, SHA-256
`1B:4F:42:8E:E4:32:5C:4E:1F:A4:C9:DE:56:41:DB:44:6D:BA:92:95:63:EC:57:E3:4F:7D:6B:D2:C2:A5:33:AF` -
and so is every later version: a file that claims to be tgarr's TV app and is signed by anything
else is not. (0.5.11 first carried `tgarr-tv-preview.apk`, signed with a throw-away key: remove
that one before installing this.)
