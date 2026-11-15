# Converting films on the graphics card

When a film is something a browser cannot play - a DVD or Blu-ray image, an avi, HEVC, an
mkv with sound it has no decoder for - tgarr converts it while it is watched. In software
that costs about two processor cores for one 1080p film on a desktop processor, and more
than a small home server has to give. A graphics card's encoder does the same for a
fraction of one core.

tgarr uses the card **by itself** when its container is given one: at the first film it
runs a one-second trial on each card it can see, and uses the first that works. If a card
refuses a film, that film is converted in software and the viewer notices nothing.
*Settings → Converting films* says what is in use and switches it off.

What was measured (two minutes of 1080p with film grain, HEVC to H.264):

| Machine | In software | On the card |
|---|---|---|
| Core i7-7700, its own graphics (HD 630) | 523 s of processor | 165 s |
| Core i9-14900KS, its own graphics (UHD 770) | 344 s | 83 s |
| the same, NVIDIA RTX 3070 Ti | 344 s | 78 s |

What remains on the card's side of the table is reading the film (HEVC, in software); the
making of the picture itself all but disappears.

## Which cards

| Card | How | Tried |
|---|---|---|
| Intel graphics, 2015 and later (HD 5xx, UHD, Iris, Arc; N100, J4125 ...) | VAAPI, through `/dev/dri` | on a 7th and a 14th generation |
| NVIDIA | NVENC, through NVIDIA's container toolkit | on an RTX 3070 Ti |
| AMD | VAAPI, through `/dev/dri` | **not tried** - the driver is in the image |
| Windows (WSL2) with an NVIDIA card | NVENC | **not tried** |
| Windows (WSL2) with Intel or AMD graphics; macOS | - | not possible: the Linux inside cannot reach the card's encoder |

## Handing the card to tgarr

Docker gives a container a card only when told to, and refuses to start one that names a
card which is not there - so it is not in the `docker-compose.yml` everybody gets.

**The Linux installer does it for you**: run it (again) and it writes
`docker-compose.override.yml` for the machine's card. If you have an override file of
your own it leaves it alone and says so; add the lines below.

**By hand** - `docker-compose.override.yml`, beside `docker-compose.yml`:

Intel or AMD:

```yaml
services:
  api:
    devices:
      - /dev/dri:/dev/dri
```

NVIDIA (the host needs the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)):

```yaml
services:
  api:
    environment:
      NVIDIA_DRIVER_CAPABILITIES: compute,video,utility
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

Then `docker compose up -d`. On Synology, QNAP, Unraid and the like the same `devices`
line is set in the container's settings ("device", "extra parameters": `--device /dev/dri`).

## Good to know

- **Picture size.** Newer cards hold the stream to a size - about 8 Mbit/s for 1080p,
  5 for 720p, 2.5 for 480p. Intel graphics of before 2019 can, with the free driver the
  image carries, only hold a quality: an ordinary film comes out at about 4 Mbit/s for 720p
  (software makes 2.4), a very grainy one much larger. On a home network that is of no
  consequence; over a slow link, choose 480p in the player, or switch to software.
- **Which card, when there are two.** Intel or AMD is tried first, then NVIDIA. To decide
  yourself: `TGARR_TRANSCODE_HW` in the container's environment - `nvenc`,
  `vaapi:/dev/dri/renderD129`, or `off`.
- A film whose picture is already H.264 is passed on untouched, as before: no card is
  involved, and nothing is lost.
