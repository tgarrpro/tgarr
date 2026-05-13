# Responsible Use & Copyright

tgarr is a content-neutral software tool. It does not host any content, does not seed any files, does not solicit infringing material, and does not encourage copyright infringement of any kind.

## What tgarr is

tgarr is **a search engine** for Telegram channels that the operator has voluntarily joined under their own Telegram user account. It indexes filenames, captions, and message identifiers — comparable in function to how Google indexes the publicly accessible web, NZBGeek indexes Usenet posts, or `youtube-dl` enumerates a public YouTube playlist.

## What tgarr is not

- tgarr is **not a content host**. **No file content is stored on tgarr.** The index stores only metadata: filename, caption, mime type, channel ID, message ID, posted timestamp. Files themselves remain entirely on Telegram's infrastructure.
- tgarr **does not warrant that indexed targets still exist**. As with any search engine, indexed entries reflect the state of the source at crawl time. By the time a user retrieves a release, the originating channel may have been deleted, banned, or otherwise made unreachable; the specific message may have been deleted by its poster; Telegram may have made the file unavailable. tgarr does not promise persistence.
- tgarr is **not a peer-to-peer network**. There is no seeding, no peer exchange, no DHT participation, no IP exposure to other users.
- tgarr is **not affiliated with Telegram**. We use Telegram's documented MTProto API as any user-account client does.
- tgarr is **not affiliated with Sonarr, Radarr, or any *arr-stack project**. We implement the Newznab API specification, which they happen to consume.

## We respect copyright

tgarr acknowledges and supports copyright as a legal framework for protecting creative work. We expect operators of tgarr to:

1. **Only index channels they have voluntarily joined**, and which they believe to be operating in compliance with Telegram's Terms of Service.
2. **Comply with the copyright law of their jurisdiction**.
3. **Use tgarr for legitimate purposes**, including but not limited to:
   - Public-domain film, audio, and text archives
   - Open-source software distribution (e.g., Linux ISOs, Homebrew bottles)
   - Language-learning material the operator has the right to access
   - Course recordings and conference talks shared by their creators
   - The operator's own creative work
   - Public-information channels (news, government data, weather)
   - Family / community / friend-group channels the operator personally administers

If indexing a specific channel would violate copyright law in your jurisdiction, **do not join or index that channel**. Removing a channel from tgarr's index is as simple as having the operator leave the channel — tgarr stops receiving its messages immediately.

## Reporting infringing channels

tgarr does not host content. Files remain on Telegram's platform. **The correct address for infringement complaints about a Telegram channel is Telegram itself**, via their documented [DMCA / takedown process](https://telegram.org/dmca).

If a rights-holder believes a Telegram channel is being indexed by tgarr instances in a way that contributes to actionable infringement, please contact us at **abuse@tgarr.me** with:

- Identification of the copyrighted work
- The Telegram channel URL or `@username`
- A good-faith statement that you are authorized to act on behalf of the copyright owner
- Your contact information

We do not operate a centralized tgarr instance — each user runs their own copy. We will:

- Publicly document the reported channel in an issue if appropriate
- Add the channel to a community-maintained `recommended-block.txt` list shipped with tgarr (operators may opt in to consume it)
- Respond to legal counsel as required

## DMCA agent

For formal DMCA notices: **abuse@tgarr.me**.

## License

This document is part of the tgarr project, released under the [MIT License](LICENSE).

This statement is **not a substitute for legal advice**. If you have specific legal questions about your jurisdiction's copyright framework, consult a qualified attorney.
