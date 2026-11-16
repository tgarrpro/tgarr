# Course lists

(中文: [README.zh.md](README.zh.md))

Every `*.json` in the folders here is a course that tgarr offers under *Media Center & PIN → Courses → Add a
course*. One folder for each language (`en/`, `de/`, `zh/`, ...): the language the material is in, not a country.

A list holds **no material**. It says where the lessons are - at the Internet Archive, or in a public Telegram
channel - and each install fetches them from there. tgarr.me hosts none of it.

## Making one

Copy `template.json.example`, name the copy after your course (`<language>/<slug>.json`) and fill it in.

| Field | |
|---|---|
| `slug` | lower-case letters, digits and hyphens; unique among all lists |
| `title`, `about` | in the language of the material |
| `lang` | the language of the material: `en`, `de`, `zh-Hans`, ... |
| `level` | the youngest age it suits: `5-8`, `8-12`, `12-16`, `16+`, or `all` |
| `subject` | `reading` (reading in one's own language), `language` (learning a foreign one), `math`, `science`, `history`, `arts`, `music`, `computing`, `life`, `other` |
| `right` | what the material is under - see below. A list without it is not loaded |
| `credit` | who made the material |

and then **one** of the two:

**Items of the Internet Archive**

```json
"parts": [{"ia": "mcguffeysfirstec00mcgu", "title": "First Reader"}, {"ia": "mcguffeyssecond00mcgu"}]
```

`ia` is the identifier of the item (the last part of `archive.org/details/...`). The parts are the course's order;
an item with several files gives several lessons, in the order of their names.

**A public Telegram channel**

```json
"channel": "your_channel", "tag": "#de_lesen1", "upto": 120
```

The lessons are the files posted in the channel, in the order they were posted - with `tag`, only the posts whose
caption carries it, so that one channel can hold many courses in many languages (begin a tag with the language:
`#zh_pinyin1`, `#de_lesen1`, `#es_lectura2`). Post the lessons in the order they are to be taken; a file's name is
the lesson's name. The channel has to be public and must **not** have "Restrict saving content" switched on: with
it on, nobody can read the channel at all.

`upto` is the number of the last post that was read when the list was checked (the number at the end of a post's
link, `t.me/your_channel/120`). The course takes the channel's posts **up to that one and no further**: whoever
may post in a channel could post anything under a tag tomorrow, and it would be on a child's screen the same day.
When lessons are added, send the list in again with a higher `upto`; they are read, and then they appear.

## Rights: what may be listed

A list states one right for all of its material:

| `right` | |
|---|---|
| `PD` | in the public domain: LibriVox recordings, Project Gutenberg texts |
| `OLD` | a printed book from before 1929, scanned by a library |
| `USGOV` | a work of the United States government |
| `NOCR` | the library that holds it states that it is not in copyright |
| `CC BY`, `CC BY-SA`, `CC BY-NC`, `CC BY-NC-SA`, `CC BY-ND`, `CC BY-NC-ND` | a Creative Commons licence given by the one who made it |

Public domain differs from country to country; what is listed here is listed under the law of the United States,
where tgarr.me is. Material you made yourself: publish it under a Creative Commons licence and say so where it is
published (in the channel's description, on the Archive item) - a licence only you know of is none.

Not listed, whatever the list says: material somebody else holds the rights to (bought courses, recordings of
lessons from a paid platform, scanned school books in copyright), and an Archive upload whose only claim to being
free is the mark its uploader gave it.

## Sending one in

Open a pull request with your file at <https://github.com/tgarrpro/tgarr>. Every list is read before it is taken:
the rights it states are checked against where the material is published, and the material is looked at for what a
family would not want in a children's library. `python api/test_courses.py` checks the form of every list here.

Lists are for children: nothing sexual, no cruelty for its own sake, no hate, no preaching.
