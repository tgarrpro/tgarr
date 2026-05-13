"""builtin_channels.py — Telegram channels recalled from training data.

All entries are marked `source: claude-knowledge`, `status: unverified`.
Health-check will resolve current state (active / dead / banned) and update
the registry. Hallucination rate is non-zero; this list is seed data, not
authoritative.

Output:
    python tools/builtin_channels.py > channels/registry-builtin.yaml
"""
import sys
import time

import yaml

E: list[dict] = []


def C(u, region="global", lang="en", cat="mixed", *, name=None,
      aud="family", media="link_only", notes=None, more_tags=None):
    item = {
        "username": u,
        "category": cat,
        "audience": aud,
        "region": region,
        "language": lang,
        "media_types": [m.strip() for m in media.split(",")] if media else [],
        "status": "unverified",
        "source": "claude-knowledge",
        "tags": [f"region:{region}", f"category:{cat}"] + (more_tags or []),
    }
    if name:
        item["display_name"] = name
    if notes:
        item["notes"] = notes
    E.append(item)


def B(usernames, region, lang, cat, *, aud="family", media="link_only", more_tags=None):
    for u in usernames.split():
        C(u, region, lang, cat, aud=aud, media=media, more_tags=more_tags)


# ════════════════════════════════════════════════════════════════════
# TELEGRAM META / OFFICIAL
# ════════════════════════════════════════════════════════════════════
C("durov", "global", "en", "tech", name="Pavel Durov", media="video,photo", notes="Telegram founder personal channel")
C("telegram", "global", "en", "tech", name="Telegram", media="photo,video", notes="Official product channel")
C("TelegramTips", "global", "en", "education", name="Telegram Tips", media="video,photo")
C("TelegramStickers", "global", "en", "tech", media="photo")
C("StickerArtist", "global", "en", "tech", media="photo")
C("ArtStickers", "global", "en", "tech", media="photo")
C("emojis", "global", "en", "tech", media="photo")
C("TelegramGeeks", "global", "en", "tech", media="link_only,photo")
C("durov_russia", "ru", "ru", "tech", name="Дуров (RU)")
B("TelegramBeta TelegramTipsRu TelegramTipsEN BotNews TONblockchain", "global", "en", "tech")

# ════════════════════════════════════════════════════════════════════
# TECH — GLOBAL ENGLISH
# ════════════════════════════════════════════════════════════════════
B("python pythonprogramming pythoncoder pythonista_dev pythonweekly pythontricks python_for_beginners", "global", "en", "tech")
B("javascript javascript_es6 javascriptweekly javascriptdaily javascript_proger js_daily", "global", "en", "tech")
B("typescriptlang reactjs reactnative reactjsfeed vue vuejs vue_js angular angular_js node nodejs_dev", "global", "en", "tech")
B("rustlang rust_lang rustdaily rustbooks golang gopath_news golangtricks go_lang", "global", "en", "tech")
B("ruby_lang rubydeveloper rails kotlin kotlin_lang dart_lang flutter_dev flutterapp", "global", "en", "tech")
B("java javadeveloper java_programmer java_world spring_world", "global", "en", "tech")
B("cpp_lang cpp_programming cplusplus c_programming c_plus_plus algorithms_dsa", "global", "en", "tech")
B("php_programmers laravel_news laravel_community symfony wordpress wordpressnews", "global", "en", "tech")
B("scala_lang scalalang elixir_lang elixir_news clojure_lang haskell_lang", "global", "en", "tech")
B("swift_lang swift_programming swiftdeveloper objectivec ios_dev iosdev android_dev androidprogramming", "global", "en", "tech")

B("archlinux ubuntu_official linux linuxnews linuxgizmos LinuxFoundation linuxacademy linuxhandbook linux_dev linuxworld", "global", "en", "tech")
B("debian_official fedora opensuse manjarolinux kalilinux_official parrot_security tails_official", "global", "en", "tech")
B("docker dockernews dockerhub kubernetes kubernetesnews k8snews helm_charts istio_io", "global", "en", "tech")
B("ansible_io terraform_io vault_hashicorp consul_io nomadhashi packer_io", "global", "en", "tech")

B("github gitlab gitea_io codeberg sourcehut bitbucket_org", "global", "en", "tech")
B("postgresql mysql_dev mariadb mongodb redis sqlite cassandra cockroachdb", "global", "en", "tech")
B("elasticsearch logstash kibana grafana prometheus_io openobserve", "global", "en", "tech")
B("nginx_org openresty apache_httpd haproxy traefik envoy_proxy", "global", "en", "tech")

B("aws aws_news azure azurenews googlecloud gcp_news digitalocean linode hetzner_news vultr", "global", "en", "tech")
B("openai chatgpt anthropic_ai claude_ai huggingface_co replicate stability_ai midjourney_news", "global", "en", "tech")
B("nvidia_ai tensorflow keras_ai pytorch_ai jax_ai scikit_learn opencv_ai", "global", "en", "tech")
B("kaggle_competitions dataisbeautiful data_science_central towardsdatascience analyticsvidhya", "global", "en", "tech")

B("hackernews hackernewsbot hacker_news hackernewsfeed yc_news ycombinator producthunt product_hunt", "global", "en", "tech")
B("theverge TheVerge techcrunch arstechnica wired engadget gizmodo mashable theinformation", "global", "en", "tech")
B("zdnet cnetnews techradar venturebeat thenextweb tomshardware anandtech tech_world", "global", "en", "tech")
B("bleepingcomputer threatpost krebsonsecurity bleepingcomputernews darkreading kaspersky_lab", "global", "en", "tech")

B("vim_users emacs_lisp neovim_users tmux_users zsh_users fish_shell oh_my_zsh", "global", "en", "tech")
B("vscode jetbrains_news intellij pycharm webstorm_news android_studio xcode_dev", "global", "en", "tech")

B("blender_3d unity3d unrealengine godotengine threejs babylonjs", "global", "en", "tech")

B("freecodecamp codewars hackerrank leetcode geeksforgeeks_official codecademy", "global", "en", "education")
B("realpython python_tutorial pythonweekly css_tricks csstricks frontendmasters", "global", "en", "education")
B("designernews dribbble behance figma_design uxmovement smashingmag", "global", "en", "tech")

B("dev_to dailyjs morningbrew_tech techbrew indiehackers", "global", "en", "tech")

# ════════════════════════════════════════════════════════════════════
# CYBERSECURITY / INFOSEC
# ════════════════════════════════════════════════════════════════════
B("infosec_news cyber_security cybersec_news securityweekly bsidescon defconchannel", "global", "en", "tech", more_tags=["security"])
B("hackerone_official bugcrowd intigriti synack_io zerodayinitiative", "global", "en", "tech", more_tags=["security"])
B("malwarebytes sentinellabs crowdstrike_alerts mandiant fireeye_news", "global", "en", "tech", more_tags=["security"])
B("ctf_news ctf_writeups capture_the_flag picoctf hackthebox tryhackme", "global", "en", "tech", more_tags=["security","ctf"])
B("offensive_security pentestmonkey pentest_news redteamops blueteam_news", "global", "en", "tech", more_tags=["security"])
B("metasploit_news burpsuite owasp_official owaspzap nmap_org wireshark_official", "global", "en", "tech", more_tags=["security"])

# ════════════════════════════════════════════════════════════════════
# NEWS — INTERNATIONAL ENGLISH
# ════════════════════════════════════════════════════════════════════
B("bbcbreaking bbcworld bbcnews bbcamerica bbc_news bbc_world", "global", "en", "news")
B("cnn cnnbreaking cnninternational cnn_international foxnews foxnewsdesk", "us", "en", "news")
B("reuters reutersworld reutersagency reuters_business reuters_tech", "global", "en", "news")
B("aljazeera_english aljazeera AlJazeeraEnglish aje_news ajenglish", "mena", "en", "news")
B("bloomberg bloombergnews bloomberglive bloomberg_business bloomberg_markets", "global", "en", "news")
B("nytimes nyt washingtonpost washpost wapo theguardian guardian_news", "global", "en", "news")
B("the_economist TheEconomist economist financial_times ft_news ftworld", "global", "en", "news")
B("ap associated_press afp_news afpfrance dpa_news", "global", "en", "news")
B("dwnews dw_english dw_english_news france24english france24_en", "global", "en", "news")
B("rt_news RT_com rt_intel sputniknews sputnikworld sputnik_news", "global", "en", "news", more_tags=["state-russia"])
B("cgtn cgtn_news cgtn_official chinadaily globaltimesnews", "global", "en", "news", more_tags=["state-china"])
B("euronews euronews_english euronewslive politico politicoeurope axios axios_news", "global", "en", "news")
B("vox_dot_com voxdotcom theatlantic atlanticmagazine newsweek time_magazine timemagazine", "global", "en", "news")
B("usatoday wsj wallstreetjournal cnbc cnbc_news cbsnews abc_news abcnews", "us", "en", "news")
B("breitbart drudge_report dailymail dailywire huffpost huffington", "global", "en", "news")
B("npr nprnews motherjones theintercept jacobinmag", "us", "en", "news")
B("skynews skynewsbreak skynewsbreaking sky_news skynewsaust", "global", "en", "news")
B("indiatoday ndtv timesofindia hindustantimes the_hindu the_print thequint scrollin", "in", "en", "news")
B("jpost timesofisrael ynetnews haaretzcom haaretz i24news", "il", "en", "news")
B("kyivindependent kyivpost kyivpost_news ukrinform ukrpravdanews", "ua", "en", "news")
B("politicokorea koreaherald koreatimes korea_jpnews", "kr", "en", "news")
B("japantimes mainichi_jp asahishimbun nhk_world", "jp", "en", "news")
B("manilatimes inquirer_news inquirerdotnet rappler abscbnnews gma_news", "ph", "en", "news")
B("scmpnews scmp_news scmptopstories south_china_morning_post", "hk", "en", "news")
B("straitstimes channelnewsasia cna_today CNAlatest", "sg", "en", "news")

# ════════════════════════════════════════════════════════════════════
# RUSSIAN / RU-LANG
# ════════════════════════════════════════════════════════════════════
B("meduzalive meduzaproject meduzaworld meduza_io meduza", "ru", "ru", "news", more_tags=["independent"])
B("mash mash_batashev breakingmash mash_donbass mash_iptash mash_siberia mash_5x5", "ru", "ru", "news")
B("baza bazabazon baza_chp baza_main bazason", "ru", "ru", "news")
B("shot_shot shot shot_24 shot_press shot_24_official", "ru", "ru", "news")
B("rian_ru ria_novosti ria ria_news tass_agency_official tassagency tass_news", "ru", "ru", "news", more_tags=["state-russia"])
B("rbc_news rbc_news_main rbcnews rbcmoney rbc_ru rbk_ru", "ru", "ru", "news")
B("kommersant kommersant_official kommersant_ru kommersantfm", "ru", "ru", "news")
B("vedomosti_official vedomosti vedomostiru", "ru", "ru", "news")
B("forbesrussia forbes_ru forbesru", "ru", "ru", "news")
B("novaya_gazeta novayagazeta novayagazetaru novaya_gazeta_europe ng_europe", "ru", "ru", "news", more_tags=["independent"])
B("rt_russian rt_ru rt_documentary rtenglish rt_dmitry rt_arabic_official", "ru", "ru", "news", more_tags=["state-russia"])
B("tvrain tvrain_official tvrain_ru telekanaltvrain", "ru", "ru", "news", more_tags=["independent"])
B("istories_media iStories importantstories istories_ru", "ru", "ru", "news", more_tags=["independent"])
B("ostorozhno_novosti ostorozhno_moscow ostorozhno_media ostorojno", "ru", "ru", "news")
B("topor_live toporlive topor topor_news topor_official", "ru", "ru", "news")
B("readovkanews readovka readovka_news readovkaru", "ru", "ru", "news")
B("rusvesna_su rusvesna rusvesna_news", "ru", "ru", "news", more_tags=["state-russia"])
B("varlamov varlamov_news ilyavarlamov", "ru", "ru", "news")
B("antifondov antifondov_official", "ru", "ru", "news")
B("arestovich arestovich_ua arestovych_official", "ua", "ru", "news")
B("nevzorov nevzorov_official nevzorovtv", "ru", "ru", "news")
B("solovievlive solovievvideo solovyev", "ru", "ru", "news", more_tags=["state-russia"])
B("simonyan margaritasimonyan", "ru", "ru", "news", more_tags=["state-russia"])
B("kadyrov_95 kadyrov_official ramzan_kadyrov_official", "ru", "ru", "news")
B("zaharovamariya MariaZakharovaOfficial zakharova_official", "ru", "ru", "news", more_tags=["state-russia"])
B("dimsmirnov175 dimsmirnov dim_smirnov_news", "ru", "ru", "news")
B("kashinguru kashin_guru", "ru", "ru", "news")
B("politjoystic political_kuhnia kuhniaeva politkuhnia", "ru", "ru", "news")
B("oldmilitary stalingulag breakingmash_24 breakingmash_official", "ru", "ru", "news")
B("ramzayiegokomanda ramzayivpered komandaramzay", "ru", "ru", "news")
B("rusbrief brief_official brief_political", "ru", "ru", "news")
B("kontekstcanvas kontekst_politico", "ru", "ru", "news")
B("ranepa ranepa_official ranepa_news", "ru", "ru", "education")
B("vesti_news vesti_ru vestidocs vestidoma vesti24", "ru", "ru", "news", more_tags=["state-russia"])
B("rg_ru rossiyskaya_gazeta rg_news", "ru", "ru", "news", more_tags=["state-russia"])
B("gazeta_ru gazetaru gazeta_news", "ru", "ru", "news")
B("kp_ru komsomolskaya_pravda kpru", "ru", "ru", "news")
B("lentaru lenta_ru lenta", "ru", "ru", "news")
B("mk_ru moskovskij_komsomolets mkkomsomol", "ru", "ru", "news")
B("topwar topwar_ru topwar_russia", "ru", "ru", "news", more_tags=["military"])
B("rusi_inform rusinform info_ru_official rusinform_ru", "ru", "ru", "news")
B("milinfolive WarGonzo wargonzo warjournal", "ru", "ru", "news", more_tags=["military"])
B("rkadyrov_95 chechnya_today grozny_today", "ru", "ru", "news")
B("ne_dvigayte ne_dvigaytes_news ne_dvigaytesnews", "ru", "ru", "news")

B("RusEconNews economyrus rusbusinessnews economy_ru", "ru", "ru", "business")
B("finansolomka prostieinvestitsii investfunds_ru smartlabnews", "ru", "ru", "finance")
B("cb_official cb_russia cbr_official banki_ru bankiru sberbank sberbank_official tinkoffjournal tinkoff_bank", "ru", "ru", "finance")

B("zoloto1tv 1tv channelone russia1 russia24 ntv ntvru rentv rentv_official tvc_official perviy", "ru", "ru", "news", more_tags=["state-russia"])

B("ru_kino sovetskoe_kino kinopoisk kino_poisk filmru russian_cinema russian_films_ru", "ru", "ru", "cinema")
B("rusianseries ruseriesfree rusfilms_archive sovkino sovetskie_filmy", "ru", "ru", "cinema")
B("kinomania_news afishaonline afisha_ru afisha", "ru", "ru", "cinema")
B("imdb_russia kinoteatr_ru kinopoiskonline okko_official kinopoiskhd ivi_ru", "ru", "ru", "cinema")

B("ru_music rusmusic russian_music russianhits russian_pop russian_rock russian_rap rustop", "ru", "ru", "music")
B("yandex_music vk_music zaycev_net", "ru", "ru", "music")

B("books_ru russian_books ru_books rusbook libraryru bookzz_ru flibusta_books", "ru", "ru", "ebook")
B("litres litres_ru ridero readrate", "ru", "ru", "ebook")
B("audiobook_ru audiobooks_russian librivox_ru", "ru", "ru", "audiobook")

B("ru_tech tech_ru itnewz tech_world_ru habr_news habr_com", "ru", "ru", "tech")
B("vc_ru vcru tj_journal hi_tech_mail tehnostudio it_world_russia", "ru", "ru", "tech")
B("cnews_ru cnews_news rusnano rosatomofficial", "ru", "ru", "tech")

# ════════════════════════════════════════════════════════════════════
# UKRAINIAN
# ════════════════════════════════════════════════════════════════════
B("ukrpravda ukrpravdanews ukrpravdanet ukrainian_pravda pravda_news_ua", "ua", "uk", "news")
B("hromadske hromadske_ua hromadske_radio liga_news liganet", "ua", "uk", "news")
B("nv_ua novoevremya nv_world", "ua", "uk", "news")
B("kyivindependent kyiv_independent kyiv_post kyivpost", "ua", "uk", "news")
B("zelenskiy_official zelenskiy_official_ua zelensky_official", "ua", "uk", "news")
B("zradapremoga zradanepremoga zrada_or_peremoga", "ua", "uk", "news")
B("bbc_ukrainian bbc_news_ukrainian", "ua", "uk", "news")
B("voa_ukrainian dw_ukrainian", "ua", "uk", "news")
B("ukrarmy ukrainianarmy general_staff_zsu ssoarmy", "ua", "uk", "news", more_tags=["military"])
B("trukha truha truha_ua truha_ukraine truha_kharkov truha_kiev", "ua", "uk", "news")
B("ukrainenowenglish ukrainenow_en ukrainerus_news", "ua", "uk", "news")
B("rada_uad rada_official rada_ua verkhovnarada", "ua", "uk", "news")

# ════════════════════════════════════════════════════════════════════
# IRAN / PERSIAN (FA)
# ════════════════════════════════════════════════════════════════════
B("BBCFarsiTV bbcfarsi BBCpersian bbcpersian_official", "ir", "fa", "news")
B("manototv manotoofficial manotonews ManotoNews mototv", "ir", "fa", "news")
B("iranintl iranintl_tv IranInternational iranintl_news iran_intl iranintl_farsi", "ir", "fa", "news")
B("voa_farsi voafarsi voanews_farsi voafarsinews", "ir", "fa", "news")
B("DW_Farsi dw_farsi dwfarsi dw_persian", "ir", "fa", "news")
B("RFI_Persian RFI_farsi rfi_farsi", "ir", "fa", "news")
B("akharinkhabar akhbar_fori akhbarfori akharin_khabar", "ir", "fa", "news")
B("farsnews fars_news farsnewsagency", "ir", "fa", "news", more_tags=["state-iran"])
B("mehrnews mehr_news mehrnewsagency", "ir", "fa", "news", more_tags=["state-iran"])
B("isnaagency isna_news isnafa isna_iran", "ir", "fa", "news", more_tags=["state-iran"])
B("tasnimnews tasnim_news tasnimnews_ir", "ir", "fa", "news", more_tags=["state-iran"])
B("mashregh_news mashreghnews mashregh", "ir", "fa", "news")
B("bartarinha bartarin bartarin_news", "ir", "fa", "news")
B("masih_alinejad masih_official masih_alinejadofficial masih_pv", "ir", "fa", "news")
B("aljazeerafarsi aljazeera_farsi aljazeera_persian", "mena", "fa", "news")
B("iran_filtarbon iran_filterbon farsi_filtarbon", "ir", "fa", "tech")
B("RadioFarda radiofarda radiofarda_ir", "ir", "fa", "news")
B("irna_news irna_official irna_ir", "ir", "fa", "news", more_tags=["state-iran"])
B("euronews_farsi euronews_persian", "ir", "fa", "news")
B("ayandehnegar ayande_negar ayandenegarmm", "ir", "fa", "news")
B("kayhan_news kayhan_official kayhan_ir", "ir", "fa", "news", more_tags=["state-iran"])
B("entekhab entekhab_ir entekhabnews", "ir", "fa", "news")
B("hamshahri hamshahri_news hamshahri_online", "ir", "fa", "news")
B("etemad_newspaper etemadonline etemad_news", "ir", "fa", "news")
B("aetemaadnews etemad_ir hamshahriarchive", "ir", "fa", "news")
B("shahrvand_news shahrvand_ir vatan_emrooz vatanemrooz", "ir", "fa", "news")
B("rajanews raja_news rajanews_ir", "ir", "fa", "news")
B("bbcuzbek bbc_uzbek bbcuzbek_news", "uz", "uz", "news")
B("bbcurdu bbc_urdu bbcurdu_news", "pk", "ur", "news")

B("filmsfa filmha_fa cinema_iran cinemairan iranian_cinema_official irancinema", "ir", "fa", "cinema")
B("kakhketan kheradnameh persianbooks persianbook irbook_official ketabkhane_irani", "ir", "fa", "ebook")
B("persianmusic persian_music musicfa music_iran iranian_music iranmusic farsibook", "ir", "fa", "music")
B("digitalsalam digikala_news ifilm_tv salam_doostan", "ir", "fa", "mixed")
B("varzesh3 varzesh_three varzesh3_news", "ir", "fa", "sport")
B("90tvplay 90tv tv_90 footballfa fotballfa", "ir", "fa", "sport")

# ════════════════════════════════════════════════════════════════════
# INDIAN / HINDI / SOUTH-ASIAN
# ════════════════════════════════════════════════════════════════════
B("ndtv ndtvindia timesofindia hindustantimes the_hindu indiatoday indianexpress dnaindia", "in", "en", "news")
B("the_print the_quint thewire_in scroll_in firstpost news18 republic_world", "in", "en", "news")
B("PTI_news ptinews ANI_News aninews ani_official", "in", "en", "news")
B("zee_news zeebharat zee5 starplus starbharat", "in", "hi", "news")
B("aajtak_news aaj_tak aajtaknews", "in", "hi", "news")
B("dainikbhaskar bhaskar_news jagran dainikjagran amarujala", "in", "hi", "news")
B("bbchindi bbcnewshindi voa_hindi voahindi", "in", "hi", "news")
B("dw_hindi", "in", "hi", "news")
B("indianexpress_hindi navbharat_times navbharattimes nbt_news", "in", "hi", "news")

B("bollywood bollywoodbubble bollywoodgossip bollywoodbuzz bollywood_news bollywood_world bollywoodgrapevine", "in", "hi", "cinema")
B("hindi_movies HindiMovies hindimovies_hd HindiMoviesHD hindicinema indianmovies indian_movies", "in", "hi", "cinema")
B("yashrajfilms yrf dharmaproductions karanjohar shahrukhkhan iamsrk iamSRK", "in", "hi", "cinema")
B("akshaykumar akshaykumarofficial salmankhan salman_khan_official aamirkhan_official aamirkhanproductions", "in", "hi", "cinema")
B("hrithikroshan ranbirkapoor ranveersingh ranveerofficial aliabhatt aliaabhattt deepikapadukone", "in", "hi", "cinema")
B("anushkasharma katrinakaif priyankachopra priyankachopraofficial kareenakapoor saraalikhan janhvikapoor kiaraadvanio", "in", "hi", "cinema")
B("tseries TSeriesOfficial tseries_official tipsofficial yashrajofficial", "in", "hi", "music")
B("zeestudios zee_studios zee_music zeemusiccompany zeemusic", "in", "hi", "music")
B("sonymusicindia sonyindia ErosNow ErosInternational erosinternational viacom18 viacom18movies", "in", "hi", "cinema")
B("primevideoIN primevideoIndia hotstarofficial disneyplus_hotstar disneyhotstar netflix_india netflixIN", "in", "hi", "cinema")

B("tamil_movies tamil_movies_hd tamilmoviesnew tamil_movies_new tamilcinemanews tamil_cinema_news kollywood kollywoodgrapevine", "in", "ta", "cinema")
B("vijaytv vijay_tv suntv sun_tv suntvnews kalaignar_news rajanetwork rajatv", "in", "ta", "cinema")
B("telugumovies telugu_movies telugu_movies_hd tollywood_news tollywood_official telugucinema telugu_cinema", "in", "te", "cinema")
B("teluguone teluguonenet etv_telugu etv_andhra mahaa_news ntv_telugu", "in", "te", "news")
B("malayalam_movies malayalammovies malayalam_cinema malayalam_films mollywoodgrapevine asianet_news", "in", "ml", "cinema")
B("kannada_movies kannadamovies kannada_cinema sandalwood_news udaya_movies", "in", "kn", "cinema")
B("bengali_movies bengalimovies bengali_cinema tollygunge bengali_tv banglafilms", "in", "bn", "cinema")
B("punjabi_movies punjabimovies punjabi_cinema pollywood_news pollywood_official", "in", "pa", "cinema")
B("gujaratimovies gujarati_cinema marathi_movies marathicinema bhojpuri_movies bhojpuricinema", "in", "hi", "cinema")

B("iplnews iplnewsofficial cricket_news cricketnews espncric cricinfo_news cricinfo iccworldcup mumbaiindians chennaisuperkings royalchallengersbangalore", "in", "en", "sport")
B("kohli viratkohli viratkohlinews msdhoni rohitsharma_official ms_dhoni rohitsharma kapil_sharma", "in", "en", "sport")

B("tech_burner geekyranjit ndtv_gadgets gadgets_360 indian_techies techworld_indian", "in", "en", "tech")
B("upsc_preparation upscpreparation upscthegate upsccoaching ias_ips_aspirants iasaspirants drishti_ias", "in", "en", "education")
B("byjus byjuofficial unacademy_official unacademy vedantu vedantulearn extramarks", "in", "en", "education")
B("jee_main_2026 jeemains neet_neet_2026 neet2026 cbsenews", "in", "en", "education")

B("indianbooks indian_books_pdf bookspdf books_pdf englishbooks ebooks_pdf", "in", "en", "ebook")

# ════════════════════════════════════════════════════════════════════
# PAKISTANI / URDU
# ════════════════════════════════════════════════════════════════════
B("dawn_com geo_news arynews jangnews dunya_news samaatv samaa_news", "pk", "ur", "news")
B("dawn_news bbcurdu voanewsurdu pak_armymedia pakarmyofficial", "pk", "ur", "news")
B("urdu_books urdu_novels urdu_poetry urdu_shayari urdu_collection", "pk", "ur", "ebook")
B("pakistan_cricket cricket_pakistan pakistancricketteam", "pk", "ur", "sport")

# ════════════════════════════════════════════════════════════════════
# CHINESE / TAIWANESE / HK (anti-CCP politics is dominant on TG)
# ════════════════════════════════════════════════════════════════════
B("chnews chinese_news chinanews_telegram cnewstoday chinese_world", "cn", "zh", "news")
B("AsianBoss asianbosschina chinapolitics chinapolitics_news chinaobserver china_observer", "cn", "zh", "news")
B("WenYunchao wenyunchao_main thinkingchinese", "cn", "zh", "news")
B("twitterhk twitterhongkong hongkongnews hong_kong_news hk_news HKFP HKFP_news", "hk", "zh", "news")
B("apple_daily_taiwan apple_daily_hk applenext applenextmedia", "hk", "zh", "news")
B("standnews_hk standhk", "hk", "zh", "news")
B("hkmag hk_courier hkcourier hkobserver hk_observer", "hk", "zh", "news")
B("twfanjingjing twitterhkrebellion hkrebellionchat", "hk", "zh", "news")
B("liaison_office_hk hkchinese_news", "hk", "zh", "news")
B("twfan_dao twitter_taiwan twnews_taiwan taiwannews_official storm_media stormmedia", "tw", "zh", "news")
B("ltnonline liberty_times etnews udnnews tsainformation taiwan_dpp", "tw", "zh", "news")
B("bbcchinese bbc_chinese bbczhongwen", "cn", "zh", "news")
B("voachinese voa_chinese voanewschinese", "cn", "zh", "news")
B("rfa_chinese rfa_mandarin rfa_cantonese rfa_tibetan rfa_uyghur", "cn", "zh", "news")
B("dw_chinese dw_zhongwen dw_china", "cn", "zh", "news")
B("storm_media stormmedia thenewslens new_bloom new_bloom_magazine", "tw", "zh", "news")

B("china_business chinaeconomicnews caixin caixin_global caixinglobal sixthtone", "cn", "zh", "business")
B("chinatech china_tech_news techcrunchchina pingwest 36kr 36krchina huxiu", "cn", "zh", "tech")
B("chinese_books chinese_ebooks chinese_novels chinese_literature_official", "cn", "zh", "ebook")
B("chinese_movies china_cinema chinacinema_news chinese_cinema_global cmovies cmovies_hd", "cn", "zh", "cinema")
B("ccav_news cctv_official cctv_news xinhua_news_official xinhuanet xinhuanet_news", "cn", "zh", "news", more_tags=["state-china"])
B("chnmovies cctv_movies hkcinema hong_kong_cinema cinephile_hk", "hk", "zh", "cinema")
B("taiwancinema tw_cinema tw_dramas twdrama taiwanidols", "tw", "zh", "cinema")
B("kpop_china kpop_zh chinese_pop cpop_world china_pop", "cn", "zh", "music")

# ════════════════════════════════════════════════════════════════════
# ARABIC / MENA
# ════════════════════════════════════════════════════════════════════
B("aljazeera aljazeera_arabic alarabiya alarabiya_news skynewsarabia", "mena", "ar", "news")
B("mbcgroup mbc1 mbcnews mbc_news mbcaction bbcarabic BBCArabic", "mena", "ar", "news")
B("France24Arabic france24_arabic dwarabic dw_arabic euronews_arabic", "mena", "ar", "news")
B("rt_arabic rtarabic_news arabicrt rt_ar", "mena", "ar", "news", more_tags=["state-russia"])
B("alquds_news alqudsnewsnet alqudsnetwork alquds_alarabi", "mena", "ar", "news")
B("alhayat_news alhayatnews_official alhayat_alarabi", "mena", "ar", "news")
B("almasryalyoum elmasry_alyoum almasry_alyoum almasryelyoum_news", "mena", "ar", "news")
B("alarab arab_news arab_news_telegram alarab_world", "mena", "ar", "news")
B("egypt_news egyptnews ahram_online ahram_official almasry alarab_official", "mena", "ar", "news")
B("ksa_news saudi_news saudigazette arab_news_eng spa_news", "mena", "ar", "news")
B("uae_news emirates_news khaleejtimes gulfnews thenationaluae", "mena", "ar", "news")
B("morocco_news lematin_ma lemonde_maroc bladi", "mena", "ar", "news")
B("algeria_news el_watan algeria_telegram", "mena", "ar", "news")
B("tunisia_news mosaiquefm tunisie_news", "mena", "ar", "news")
B("lebanon_news annahar_lb lbci_news mtv_lebanon almayadeen", "mena", "ar", "news")
B("syria_news sana_sy sananews syriaNN", "mena", "ar", "news")
B("iraq_news_official alsumaria almadanews shafaaq", "mena", "ar", "news")
B("yemen_news almasdar_yemen yemen_telegram", "mena", "ar", "news")
B("palestineinformation palestine_news shehab_news shehab_agency wafa_news", "mena", "ar", "news")
B("israel_arabic israel_haaretz", "mena", "ar", "news")

B("ar_movies arabic_movies arabmovies arab_movies_hd arab_cinema arabiccinema", "mena", "ar", "cinema")
B("ar_books arabic_books arabicbooks arabic_ebooks arabicebooks", "mena", "ar", "ebook")
B("ar_music arabicmusic arabic_music_hub arab_music_world", "mena", "ar", "music")

# ════════════════════════════════════════════════════════════════════
# TURKISH
# ════════════════════════════════════════════════════════════════════
B("hurriyet hurriyet_news milliyet sabah ntvspor ntv_news cumhuriyet trtworld trthaber", "tr", "tr", "news")
B("anadoluajansi anadoluajansi_news anadoluajansi_ingilizce aa_news", "tr", "tr", "news")
B("bbcturkce bbc_turkce voa_turkce dw_turkce", "tr", "tr", "news")
B("turkish_movies turkishmovies turkish_series turkish_dizi turkdramalar", "tr", "tr", "cinema")
B("turkishmusic_official", "tr", "tr", "music")

# ════════════════════════════════════════════════════════════════════
# SPANISH / LATAM
# ════════════════════════════════════════════════════════════════════
B("bbc_mundo bbcmundo cnn_espanol cnnespanol ap_espanol afp_espanol", "es", "es", "news")
B("el_pais elpaisnews elpaisespana elpais_internacional", "es", "es", "news")
B("elmundo el_mundo_es abc_es 20minutos lavanguardia eldiario_es", "es", "es", "news")
B("lanacion lanacion_ar laciencianoarg pagina12 clarin clarincom infobae", "ar", "es", "news")
B("eltiempo eltiempo_co eluniversal elheraldoco elcolombiano", "co", "es", "news")
B("eluniversal_mx milenio reforma proceso elfinanciero universalmx aristegui aristegui_noticias", "mx", "es", "news")
B("emol elmercurio_chile latercera biobiochile cooperativacl", "cl", "es", "news")
B("elcomercio_pe latercera elperuano peru21 elcomerciope", "pe", "es", "news")
B("eltiempove eluniversal_ve voz_de_america voz_de_america_latina vozdeamerica", "ve", "es", "news")
B("dw_espanol euronews_espanol france24_espanol rt_espanol", "es", "es", "news")
B("dw_es rt_es sputnik_mundo", "es", "es", "news", more_tags=["state-russia"])

B("cine_latino cine_espanol cine_es spanish_movies movies_in_spanish peliculas peliculasonline peliculas_hd peliculasonline_es", "es", "es", "cinema")
B("series_en_espanol seriesenlatino latinoamericaseries hbomaxlatam netflixespanol netflix_espanol disneylatam", "es", "es", "cinema")
B("salsa_latina musica_latina latinmusic latin_music spanish_music musica_espanol", "es", "es", "music")
B("reggaeton reggaeton_news reggaeton_world urbanmusic_latin", "es", "es", "music")

# ════════════════════════════════════════════════════════════════════
# PORTUGUESE / BRAZILIAN
# ════════════════════════════════════════════════════════════════════
B("globo globoplay g1 estadao folhadespaulo veja terraonline uol_news", "br", "pt", "news")
B("bbc_brasil bbcbrasil dw_brasil france24_pt rt_em_portugues", "br", "pt", "news")
B("oantagonista jornalismodaequipe omarmuratto camarotv senadotv", "br", "pt", "news")
B("estadao_news folha_news folha_de_sao_paulo carta_capital cartacapital exame_news", "br", "pt", "news")

B("filmes_em_portugues filmesbr filmes_brasil_hd movies_pt filmes_dublados", "br", "pt", "cinema")
B("series_brasil seriesbrasil seriespt seriesbr_official", "br", "pt", "cinema")
B("musica_brasileira brazilmusic_official musica_br", "br", "pt", "music")
B("portuguesemovies portuguese_movies cinema_portugues", "pt", "pt", "cinema")
B("rtp_noticias rtp_oficial rtp", "pt", "pt", "news")
B("publico_jornal publico expresso_pt expresso", "pt", "pt", "news")

# ════════════════════════════════════════════════════════════════════
# GERMAN / FRENCH / ITALIAN / EUROPEAN
# ════════════════════════════════════════════════════════════════════
B("bild der_spiegel spiegelonline focus_online welt_news ntvde tagesschau zdf_news", "de", "de", "news")
B("dw_deutsch dwdeutsch bbcgerman bbc_german france24_de", "de", "de", "news")
B("rtde rt_deutsch rt_de", "de", "de", "news", more_tags=["state-russia"])
B("lemonde_fr lemonde_news france24 le_figaro lefigaro liberation_fr lemondefr lopinion_fr", "fr", "fr", "news")
B("bfmtv tf1info franceinfo rfi_fr france3regions arte", "fr", "fr", "news")
B("rmcnews lci lci_news liberation valeursactuelles", "fr", "fr", "news")
B("rt_fr rt_francais france_rt rtfrance", "fr", "fr", "news", more_tags=["state-russia"])
B("corriere corrieredellasera repubblica la_stampa lastampa ilfattoquotidiano gazzettait gazzetta_news", "it", "it", "news")
B("rai_news raiuno rai_italia rai_tg rainews24", "it", "it", "news")
B("dw_italian dwitalian rt_italia", "it", "it", "news")

B("italian_movies italian_cinema italianfilms cinema_italiano", "it", "it", "cinema")
B("french_movies french_cinema cinemafrancais cinemafr", "fr", "fr", "cinema")
B("german_movies german_cinema kino_de deutschefilme", "de", "de", "cinema")

# Nordic
B("svtnews dn_se sverigesradio expressen aftonbladet", "se", "sv", "news")
B("dr_dk dr_news politiken berlingske", "dk", "da", "news")
B("nrkno nrk_news aftenposten vg_no dagbladet_no", "no", "no", "news")
B("yle_uutiset yle_news hs_fi helsingin_sanomat", "fi", "fi", "news")

# Eastern Europe
B("kresy_pl kresy24 polskaagenda polsatnews tvn24 tvn24_news", "pl", "pl", "news")
B("dnes_cz dnes_news idnes lidovky novinky", "cz", "cs", "news")
B("origo origo_hu hvg_news hvg index_hu telex_hu", "hu", "hu", "news")
B("digi24 stirileprotv g4media adevarul_ro hotnews_ro hotnews", "ro", "ro", "news")
B("bnr_news bnr_bulgaria dnevnik_bg vesti_bg", "bg", "bg", "news")

# ════════════════════════════════════════════════════════════════════
# CRYPTO / WEB3 / BLOCKCHAIN
# ════════════════════════════════════════════════════════════════════
B("bitcoin BitcoinMagazine bitcoincore bitcoinnews bitcoinist", "global", "en", "crypto")
B("ethereum ethereum_classic vitalik vitalikbuterin ethereumofficial eth_research", "global", "en", "crypto")
B("solana solanaofficial cardano cardanoofficial polkadot polkadotnetwork tezos avalancheavax", "global", "en", "crypto")
B("polygontechnology polygon_news cosmos_news cosmoshub neartoken near_protocol algorand stellar_official", "global", "en", "crypto")
B("ripplenet xrp ripple_news litecoin dogecoin shibainuofficial shibtoken", "global", "en", "crypto")

B("binance_announcements binanceexchange BinanceExchange BinanceAcademy BinanceListing binance_us binance_global", "global", "en", "crypto")
B("kucoinexchange KuCoinExchange KuCoinAnnouncements bybit_announcement bybit_official BybitOfficial bybit_news", "global", "en", "crypto")
B("OKXofficial okxannouncements okx_official bingx_official bitget_announcement gateioglobal gate_io GateioGlobal", "global", "en", "crypto")
B("huobi_global Huobi_global crypto_com CryptoComOfficial cryptocomexchange CryptoComExchange", "global", "en", "crypto")
B("bitfinex bitfinex_official bitstampnews coinbase coinbase_news coinbase_announcements", "global", "en", "crypto")
B("dydx_news_official jupiter_dao opensea_announcements rabbithole metamask_news 1inchofficial uniswap_news pancakeswap_official", "global", "en", "crypto")

B("cointelegraph cointelegraph_news cointelegraph_chinese cointelegraph_es coindesk CoinDesk coindesk_news cryptocompare", "global", "en", "crypto")
B("coingecko_news CoinGeckonews cmc_official coinmarketcap coinmarketcap_announcement", "global", "en", "crypto")
B("the_block thekind_block the_block_crypto The_Block_Crypto theblockcrypto theblock_news", "global", "en", "crypto")
B("decryptmedia decrypt_news cryptoslate cryptobriefing CryptoBriefing", "global", "en", "crypto")
B("blockworks blockworks_news bitcoinnews_official altcoinbuzznews altcoindaily AltcoinDaily AltcoinBuzz", "global", "en", "crypto")
B("cryptopanic CryptoPanic cryptopanicnews wublockchain wublockchain_en WuBlockchain WuBlockchainEnglish", "global", "en", "crypto")

B("whale_alert whalealertbot whale_alerts chainalysisalerts ChainalysisAlerts ofac_alerts elliptic_news", "global", "en", "crypto")
B("etherscan_news bscscan_news arbiscan_news polygonscan_news basescan_news debank_news zapper_news zerion_news", "global", "en", "crypto")
B("defipulse defillama defiprime defi_news defi_world DeFiNews DefiNews DeFiBlogs", "global", "en", "crypto")
B("rektnewsletter rekthq rekt_news cryptopolitan messari_io messari_news", "global", "en", "crypto")

B("cz_binance heliumnetwork mantleprotocol arbitrumofficial optimismofficial basechain_official optimism_news arbitrum_news", "global", "en", "crypto")
B("vitalik_btc justinsuntron justin_sun changpengzhao crypto_oga crypto_kaleo cryptotraders cryptotrader_pro", "global", "en", "crypto")

# Crypto signals (many fake/scam — keep low priority)
B("crypto_signals cryptosignals cryptosignal cryptotrade_signals cryptotradingideas cryptotradingclub crypto_pump_signals", "global", "en", "crypto")
B("bitcoinpumps cryptotrade signals_crypto cryptomonday cryptotwitter cryptotraderscentral cryptotwitterlive", "global", "en", "crypto")

B("nft_news NFTnews nfts_news nftworld NFTworld nftcommunity nftartists nftartist nft_collectors", "global", "en", "crypto")
B("openseanft opensea_news raribleorg lookrare_news magic_eden_news blur_io_news", "global", "en", "crypto")

B("crypto_russia ru_crypto crypto_ru cryptocurrency_russia bitcoinmagazinerussia", "ru", "ru", "crypto")
B("cryptochina crypto_chinese chinacrypto", "cn", "zh", "crypto")
B("crypto_india_official indian_crypto cryptoindia cryptokanoon", "in", "en", "crypto")
B("crypto_arab arabcrypto crypto_mena bitcoin_mena", "mena", "ar", "crypto")
B("crypto_iran iran_crypto persian_crypto bitcoin_iran", "ir", "fa", "crypto")
B("crypto_brasil brazilcrypto crypto_brazil crypto_latam", "br", "pt", "crypto")
B("crypto_es es_crypto cryptoespanol bitcoinespanol", "es", "es", "crypto")
B("crypto_turkey turkeycrypto crypto_tr btcturk_news", "tr", "tr", "crypto")
B("crypto_korea cryptokorea_news crypto_kr upbit_news", "kr", "ko", "crypto")
B("crypto_japan crypto_jp japan_crypto coincheck_news bitflyer_news", "jp", "ja", "crypto")

# ════════════════════════════════════════════════════════════════════
# FINANCE / STOCKS / BUSINESS
# ════════════════════════════════════════════════════════════════════
B("wsb wallstreetbets wallstreetbetsteleg stockmarket_news stocks stockstotrade stocknews", "us", "en", "finance")
B("cnbc_news cnbcfastmoney bloombergmarkets ftmarkets reutersmarkets markets_news_world", "global", "en", "finance")
B("federalreserve federal_reserve federalreserve_news fed_news ecb_news ecbofficial bankofengland", "global", "en", "finance")
B("imf_news worldbank_news worldbank_official bisnews bis_org wto_news", "global", "en", "finance")
B("yahoofinance investing_com investing tradingview tradingview_official seekingalpha seeking_alpha", "global", "en", "finance")
B("zerohedge zero_hedge thedailyshot dailyshot_news mishtalk", "global", "en", "finance")
B("financialtimes_official ft_alphaville fortune_news barrons forbes_news Forbes business_insider businessinsider", "global", "en", "finance")
B("wallstreetjournal_official cnbcmarkets reutersmarkets reuters_business marketwatch", "global", "en", "finance")

B("forex_signals forex_trading forex_pips fxstreetnews fxstreet babypips dailyfx", "global", "en", "finance")
B("goldsilver_news kitco kitco_news gold_news silver_news commoditiesnews", "global", "en", "finance")
B("oilprice oilprice_news oilprice_com energy_news_global energyintel", "global", "en", "finance")

B("zerodha_news zerodhaonline upstoxofficial groww_official angelone_news icicibank_news", "in", "en", "finance")
B("indianstockmarket nse_official bse_india indian_stock_news indian_markets nifty50_news nifty50", "in", "en", "finance")
B("chinesestocks chinastocks shanghai_composite shenzhen_market", "cn", "zh", "finance")
B("russian_stocks moex_official moex_news vtb_news sberbank_news alfabank_news", "ru", "ru", "finance")
B("nikkei_news nikkei_asia nikkeiasia tokyo_markets jpx_news", "jp", "ja", "finance")
B("kospinews KRX_news koreafinance", "kr", "ko", "finance")

# ════════════════════════════════════════════════════════════════════
# MOVIES / TV / STREAMING
# ════════════════════════════════════════════════════════════════════
B("imdb rottentomatoes letterboxd metacritic mubi_news mubi criterion criterion_collection", "global", "en", "cinema")
B("netflix netflixfilm netflix_official netflixus netflixqueue netflixafrica netflixanime netflixfamily", "global", "en", "cinema")
B("amazonprime amazonprimevideo primevideo primevideous primevideouk", "global", "en", "cinema")
B("disneyplus disney_plus disneyofficial disneyfilm pixar marvel marvelstudios starwars", "global", "en", "cinema")
B("hbo hbomax hbomaxofficial appletv apple_tv_plus apple_tv hulu hulu_official", "global", "en", "cinema")
B("paramountplus paramountnetwork paramount_pictures sony_pictures sonypictures universal_pictures warnerbros warnerbrosstudios", "global", "en", "cinema")
B("a24 a24films neon_films focus_features lionsgate searchlightpictures bleecker_street", "global", "en", "cinema")

B("oscars_official theacademy oscars goldenglobes baftaofficial bafta cannesfilmfestival sundance sundanceinstitute", "global", "en", "cinema")
B("filmfestival venicefilmfestival berlinale tribecafilm cannes_film_festival tiff_net", "global", "en", "cinema")

B("cinemacat cinemaeden cinema_news cinephile filmcritics filmtwitter", "global", "en", "cinema")
B("cinemablend variety variety_news hollywoodreporter thehollywoodreporter deadline deadlineNews", "global", "en", "cinema")
B("indiewire indie_wire cinema_indie cinephiles_unite letterboxd_official", "global", "en", "cinema")

B("movies_world worldofcinema cinemaworld cinema_archive movie_archive moviearchive", "global", "en", "cinema")
B("movieshub HDMoviesHub_Official MoviesZoneOfficial MovieZoneOfficial moviezone_telegram", "global", "en", "cinema")
B("HollywoodMoviesHD hollywood_movies_hd HollywoodFilm hollywood_news hollywood_official hollywoodbubble", "global", "en", "cinema")
B("4kmovies movies4k movie4k movies_4k 4kfilms uhd_movies UHDmovies movies_uhd", "global", "en", "cinema")
B("anime_movies anime_films animehub anime_official animeworld_news animeworld_telegram", "global", "en", "anime")
B("anime_eng anime_dubbed anime_subbed AniwaveTV crunchyrollnews funimation crunchyroll_official", "global", "en", "anime")

# TV series
B("seriesofficial serieshub seriestv_world TVSeriesHD tvseries_hd tvseries_world tv_series_world", "global", "en", "cinema")
B("breakingbad walter_white better_call_saul better_call_saul_official succession_hbo succession_official", "global", "en", "cinema")
B("game_of_thrones gameofthrones_official house_of_the_dragon strangerthings stranger_things_official", "global", "en", "cinema")
B("the_office friends_official seinfeld_official the_simpsons simpsons_official rick_and_morty rickandmorty", "global", "en", "cinema")
B("the_witcher_official witcher_official netflix_witcher money_heist_official la_casa_de_papel narcos_official", "global", "en", "cinema")
B("the_mandalorian mandalorian_official star_wars_news star_wars_official mandalorian_news boba_fett_news", "global", "en", "cinema")
B("doctor_who_official sherlock_official peaky_blinders peakyblinders_official downton_abbey", "global", "en", "cinema")

# Anime / manga
B("anime_news animenewsnetwork myanimelist mal_official myanimelist_official kissanime_news", "global", "en", "anime")
B("crunchyroll funimation crunchyrolloffical wakanim anime_jp animenews mangaplus jumpplus", "global", "en", "anime")
B("manga_official onepiece_official narutoshippuden bleach_official attack_on_titan_official jjk_official jujutsu_kaisen", "global", "en", "anime")
B("dragon_ball dragonball_official dbz_official one_piece my_hero_academia mha_official hxh_official", "global", "en", "anime")
B("studioghibli ghibli_films ghibli_official kyoanimation kyoaniofficial trigger_official", "global", "en", "anime")
B("naruto narutopedia bleach hunterxhunter death_note tokyoghoul fate_series fairytail", "global", "en", "anime")
B("manga_world mangahub manga_archive mangakawaii manga_translations_eng", "global", "en", "anime")

# ════════════════════════════════════════════════════════════════════
# MUSIC / AUDIO
# ════════════════════════════════════════════════════════════════════
B("spotify spotifyofficial spotify_news spotify_charts AppleMusic apple_music apple_music_official", "global", "en", "music")
B("youtubemusic youtube_music_official tidalmusic Tidal tidal_official deezer_official soundcloud bandcamp", "global", "en", "music")
B("billboard rollingstone variety_music musicbusinessnews musicbiz musicweek", "global", "en", "music")
B("pitchfork pitchfork_news rateyourmusic rateyourmusic_official rym discogs allmusic", "global", "en", "music")

B("kpop kpop_news kpop_world kpoplife kpopfanchannel bts bts_official btsofficial bts_army", "global", "en", "music")
B("blackpink blackpinkofficial twice_official redvelvet stray_kids straykids enhypen newjeans aespa itzy", "global", "en", "music")

B("hiphop hiphopnews hiphop_world hiphopofficial rap_news rapworld worldstarhiphop xxlmag", "global", "en", "music")
B("country_music countrymusic country_music_news cma_country", "us", "en", "music")
B("edm edm_news edm_world edmnewsofficial trance_music techno_music electronic_music", "global", "en", "music")
B("classical_music classicalmusic classical_archives operanews operaworld", "global", "en", "music")
B("jazz_music jazz_world jazz_archive jazznews", "global", "en", "music")
B("metal_music metalmusic_world metalblade metalhammer metalsucks", "global", "en", "music")
B("rock_news rocknews rock_world classic_rock classicrock", "global", "en", "music")

# ════════════════════════════════════════════════════════════════════
# GAMING
# ════════════════════════════════════════════════════════════════════
B("steam_news steam_official steamcommunity steam_db steamdb steamcharts", "global", "en", "game")
B("epicgames epic_games epicgames_news epicstore epicgamesstore", "global", "en", "game")
B("xboxofficial xbox xboxlive xboxgamepass xbox_news playstation playstation_official ps_news", "global", "en", "game")
B("nintendo nintendoofficial nintendolifenews nintendoswitchnews", "global", "en", "game")
B("riotgames riot_games valve valve_news valvenews bethesda bethesda_news ubisoft ubisoft_news", "global", "en", "game")
B("ea ea_games electronicarts EA_SPORTS ea_sports activision activisionofficial blizzardent blizzard_news", "global", "en", "game")

B("ign ignnews ignlive gamespot gamespot_news polygon polygon_news kotaku eurogamer pcgamer_news", "global", "en", "game")
B("rockpapershotgun gamesindustry gamesindustrybiz gamasutra GamasutraNews", "global", "en", "game")

B("dota2news dota2_official csgo_news csgo cs2_official counterstrikenews valorant_news leagueoflegends lolesports", "global", "en", "game")
B("fortnite_news fortnite_official fortnite minecraft minecraft_news roblox roblox_news", "global", "en", "game")
B("genshin_impact genshin_impact_official honkai genshin_news hsr_news honkai_starrail", "global", "en", "game")
B("gta_news gta_official gtavi gta6_news rdr2_news", "global", "en", "game")
B("eldenring elden_ring darksoulsnews fromsoftware nintendoofficial mariobros zeldaseries pokemon pokemon_official", "global", "en", "game")
B("apexlegends apexlegends_news destiny2 destiny2_news warzone_news callofduty_news callofduty cod_news", "global", "en", "game")

B("mobilegamesnews mobile_games_world mobilegamernews gachagaming_news pubg pubgmobile pubg_news", "global", "en", "game")
B("gameleaks gamesleaks gaming_leaks gamingnews_24 gaming24 indie_games_official indiegame indiegamesnews", "global", "en", "game")

B("gamersnexus digitalfoundry hardwareunboxed lineartechtips ltt techlinkedyt MKBHD_news", "global", "en", "tech")
B("ps5 ps5_news playstationlifestyle xboxseries xbox_series xboxseriesx xbox_series_x", "global", "en", "game")

# ════════════════════════════════════════════════════════════════════
# SPORTS
# ════════════════════════════════════════════════════════════════════
B("fifa fifaofficial fifaworldcup fifawomensworldcup fifa_news uefa uefachampionsleague championsleague", "global", "en", "sport")
B("premierleague premierleague_news laliga laliga_official seriaA serieaofficial bundesliga bundesliga_news ligue1", "global", "en", "sport")
B("nba nba_news NBA nbaonline nbaofficial nbatv mlb mlb_news nhl nhl_news nfl nfl_news", "us", "en", "sport")
B("formula1 f1official f1_news f1pirelli motogp motogp_news indycar nascar nascar_news", "global", "en", "sport")
B("tennis_official atp_tour wta wta_official atp_news rolandgarros wimbledon wimbledon_official", "global", "en", "sport")

B("fcbarcelona realmadrid realmadridcf manchesterunited manunited manchestercity manchester_city liverpoolfc chelseafc arsenal", "global", "en", "sport")
B("psg parissaintgermain bayern_munich bayernmunich juventus juventus_official inter intermilan acmilan napoli", "global", "en", "sport")
B("messi cristiano cristianoronaldo neymarjr mbappe lebronjames stephencurry kevindurant", "global", "en", "sport")

B("bbcsport skysports skysportsnews espn espnnews espnplus sportsillustrated sportsillustrated_news", "global", "en", "sport")
B("goal goalcom goalnews 90min eyefootball sports365 thescore_news athleticnews theathletic", "global", "en", "sport")

B("ufcofficial ufc_news ufc bellator boxing_news boxingscene boxnationtv", "global", "en", "sport")

# ════════════════════════════════════════════════════════════════════
# SCIENCE / SPACE
# ════════════════════════════════════════════════════════════════════
B("nasa nasa_news nasajpl jpl_news esa esaofficial roscosmos_official cnsa_official", "global", "en", "science")
B("spacex spacex_news spacexofficial blueorigin blueorigin_news rocketlab_news isro_official isronews", "global", "en", "science")
B("astronomy astronomy_picture apod nasa_apod hubble_space hubble_telescope jwst webb_telescope", "global", "en", "science")
B("scientific_american sciam newscientist new_scientist nature_journal naturejournal", "global", "en", "science")
B("science_magazine sciencemag scimag_news sciencealert sciencedaily scienceeurope mit_news", "global", "en", "science")
B("popular_science popsci_news pcmag_science wired_science discovermagazine smithsonianmag", "global", "en", "science")
B("physics_today physicsworld arxiv_org arxiv_news preprintsorg", "global", "en", "science")
B("ai_news ai_research aiindex ai_today machinelearning machinelearning_news ml_news deeplearning_ai", "global", "en", "science")
B("biology_news biologyworld geneticist genomeweb evolution_news evolution_world", "global", "en", "science")
B("climate_change climatechange climatechange_news climateprotection ipcc_news", "global", "en", "science")
B("medical_news medicine_news medpagetoday statnews_official statnews medscape medscape_news", "global", "en", "science")
B("nih_news nih_official nlm_news cdcofficial cdc_news WHO who_official who_world", "global", "en", "science")

# ════════════════════════════════════════════════════════════════════
# EDUCATION / COURSES
# ════════════════════════════════════════════════════════════════════
B("coursera courseraplus edx_org edxofficial udemy udemynews udacity udacityofficial pluralsight LinkedInLearning", "global", "en", "education")
B("khanacademy KhanAcademy khanacademynews ted TEDtalks TED tedx tedxtalks", "global", "en", "education")
B("duolingo duolingo_official memrise memrise_official rosettastone babbel_official", "global", "en", "education")
B("brilliant_official brilliant_org wolfram_alpha wolframalpha quora_news quora", "global", "en", "education")
B("itunesu itunesu_news mitocw mit_ocw stanford_online stanfordonline harvardonline harvardx", "global", "en", "education")
B("alison_courses futurelearn futurelearn_official skillshare skillshare_news skillshare_official", "global", "en", "education")
B("freecodecamp freecodecampnews codecademy_official codeacademy datacamp datacamp_news", "global", "en", "education")

# ════════════════════════════════════════════════════════════════════
# EBOOKS / READING
# ════════════════════════════════════════════════════════════════════
B("books bookworld bookworld_pdf englishbooks ebooks_pdf ebookpdf english_ebooks free_ebooks_pdf free_ebooks bookhub_pdf", "global", "en", "ebook")
B("projectgutenberg gutenberg openlibrary internetarchive standardebooks libgen libgen_news z_library zlibrary annas_archive", "global", "en", "ebook")
B("goodreads goodreads_books bookstagram goodreadsofficial bookriot booksandcoffee bookrecs", "global", "en", "ebook")
B("audiobook_world audiobooks audible audible_official audiblenews libroFM librivox librivox_audiobooks", "global", "en", "audiobook")
B("nonfiction nonfictionbooks nonfiction_books fictionbooks fiction_books science_fiction_books fantasybooks fantasy_books", "global", "en", "ebook")
B("selfhelpbooks self_help_books self_improvement personal_development business_books business_classics", "global", "en", "ebook")

# ════════════════════════════════════════════════════════════════════
# PHOTOGRAPHY / ART / DESIGN
# ════════════════════════════════════════════════════════════════════
B("nationalgeographic natgeo natgeotravel natgeo_news unsplash unsplash_photos pexels_photos pixabay_official", "global", "en", "art")
B("500px 500px_news behance behance_news dribbble dribbble_news figma_design figma_news", "global", "en", "art")
B("photography_news photographyworld photography_master photographyhub photographyforum dpreview photographylifenews", "global", "en", "art")
B("artstation_news artstation art_world art_archive moma metmuseum tate_official tate_news guggenheim_news", "global", "en", "art")
B("designernews designer_world ui_design ux_design uxdesign uxmovement smashing_mag smashingmag", "global", "en", "art")
B("street_photography street_photo photography_streets streetphotographylife streetphotography_world", "global", "en", "art")
B("wallpapers wallpaper4k wallpaper_4k_official wallpaperworld wallpapers_hd 4k_wallpapers", "global", "en", "art", media="photo")

# ════════════════════════════════════════════════════════════════════
# HUMOR / MEMES
# ════════════════════════════════════════════════════════════════════
B("memes meme_world memesdaily meme_archive memearchive dankmemes dank_memes 9gag 9gag_official", "global", "en", "humor", media="photo,video")
B("reddit_memes reddit_archive worldstarcomedy ifunny_news funny_videos funnymemes", "global", "en", "humor", media="video,photo")
B("englishhumor englishjokes humorworld humor_world humorgallery", "global", "en", "humor")
B("memes_russian russianmemes russian_memes memepoland polishmemes pl_memes", "ru", "ru", "humor")
B("memes_brasil memes_br brazilmemes memesbr memes_brazil", "br", "pt", "humor")
B("memes_in indianmemes indian_memes india_memes memesindian", "in", "en", "humor")
B("arab_memes memes_arabic arabichumor memesarabic", "mena", "ar", "humor")
B("chinese_memes memes_chinese cnmemes", "cn", "zh", "humor")

# ════════════════════════════════════════════════════════════════════
# TRAVEL / LIFESTYLE
# ════════════════════════════════════════════════════════════════════
B("travel_news travel_world travel_pic travelphoto travel_photography lonelyplanet lonely_planet conde_nast_traveler", "global", "en", "travel")
B("airbnb_news booking_news tripadvisor_news skyscanner_news kayak_news expedia_news", "global", "en", "travel")
B("nationalparks national_park_service worldwonders worldheritage unesco_news", "global", "en", "travel")

B("fashion_world fashionnews fashion_news vogue voguemagazine harpersbazaar elle marieclaire_news", "global", "en", "fashion")
B("vogue_paris vogueparis vogue_uk vogue_italia vogueitalia vogue_us", "global", "en", "fashion")

B("foodnews foodnetwork bonappetit bon_appetit eater epicurious cookbook_news cookingmagazine", "global", "en", "food")
B("recipes recipe_world cooking_recipes recipearchive cook_with_us simplyrecipes", "global", "en", "food")
B("vegan_recipes vegetarian_recipes plant_based_news plantbased_recipes", "global", "en", "food")

B("health_news healthworld healthnews mensjournal womenshealth healthline_news mayoclinic_news webmd_news", "global", "en", "health")
B("fitness fitness_world bodybuilding_news menshealth menshealth_official mensphysique", "global", "en", "health")
B("yoga_news yogajournal mindbodygreen self_care selfcare meditationdaily", "global", "en", "health")
B("running_news runnersworld marathon_news triathlonworld cycling_news bikeradar", "global", "en", "health")

# ════════════════════════════════════════════════════════════════════
# RELIGION / SPIRITUAL
# ════════════════════════════════════════════════════════════════════
B("vatican_news vaticanofficial vaticanradio pope_francis catholicchurchnews catholicworld", "global", "en", "religion")
B("orthodoxchurch orthodoxy_news russian_orthodox patriarchia_ru patriarch_kirill", "global", "en", "religion")
B("christian_news christianpost christianitytoday relevant_magazine bibleverses biblestudy bible_official", "global", "en", "religion")
B("islam islamic_news islamworld islamicart aljazeera_islam islamonline aboutislam muslim_world muslimnews", "global", "en", "religion")
B("hadith hadithoftheday quran quran_pak quran_recitation quranreaders al_quran_kareem", "global", "en", "religion")
B("buddhism buddhism_news mindfulness_news dalai_lama dalailama dalailamaofficial", "global", "en", "religion")
B("hinduism hindu_news hindu_world hindutva_news indian_temples shrimadbhagavad", "in", "en", "religion")
B("sikhism sikh_news sikh_world sikhpride sikhheritage", "in", "en", "religion")
B("jewish_news jewishnews jewishworld jewishpress haaretz_jewish chabad chabadlubavitch", "global", "en", "religion")

# ════════════════════════════════════════════════════════════════════
# LEGAL / POLITICS / ANALYSIS
# ════════════════════════════════════════════════════════════════════
B("politicsnews politico_news politicoeurope politicowashington capitolhill_news washingtonexaminer", "us", "en", "news")
B("foreign_affairs foreignaffairsmag foreignpolicy foreign_policy carnegieendowment carnegie_news brookings brookings_news", "global", "en", "news")
B("rand_corp rand_news cfr_news cfr_official cfrforeignaffairs heritage_foundation heritageaction", "global", "en", "news")
B("atlanticcouncil atlantic_council chatham_house chatham_house_news rusi_org rusi_news", "global", "en", "news")
B("amnesty amnesty_international amnesty_news amnestyusa hrw hrw_news humanrightswatch", "global", "en", "news")
B("transparencyint transparency_international transparency_news icij icij_news", "global", "en", "news")

B("supreme_court supremecourt scotusblog scotus_news scotus_news_official supremecourtus", "us", "en", "news")
B("federal_court federaljudiciary uscourts uscourts_news lawfareblog lawfare_news", "us", "en", "news")
B("lawnews lawnews_world abajournal nationallaw lawnewspaper", "global", "en", "news")

# ════════════════════════════════════════════════════════════════════
# CARS / AUTOMOTIVE / TRANSPORTATION
# ════════════════════════════════════════════════════════════════════
B("cars_news caranddriver car_and_driver motortrend autoblog jalopnik autoweek autoexpress topgear", "global", "en", "auto")
B("tesla teslaofficial elonmusk tesla_motors tesla_news electric_cars electric_vehicles", "global", "en", "auto")
B("bmw bmw_official bmwmnews bmwgroup mercedes mercedes_benz mercedes_benz_official audi audi_official", "global", "en", "auto")
B("ferrari ferrariofficial lamborghini lamborghiniofficial porsche porscheofficial mclaren mclarennews", "global", "en", "auto")
B("ford ford_official chevrolet chevroletnews toyota toyotaofficial honda hondaofficial nissan nissanofficial", "global", "en", "auto")
B("ev_news evnews evworld_news evworld_official electrek InsideEVs greencarreports", "global", "en", "auto")

# ════════════════════════════════════════════════════════════════════
# OPEN SOURCE / SELF-HOSTED COMMUNITIES (high relevance to Tom)
# ════════════════════════════════════════════════════════════════════
B("selfhosted selfhostedhq self_hosted homelab homelabnews homelab_news homelab_official", "global", "en", "tech")
B("synologyofficial synology_news synology_dsm qnap qnap_news truenas truenas_scale unraid unraid_news", "global", "en", "tech")
B("homeassistant home_assistant homeassistant_news openhab openhab_news domoticz domoticz_news", "global", "en", "tech")
B("plex plex_news plexpassusers jellyfin jellyfin_official emby emby_news kodi kodi_official", "global", "en", "tech")
B("sonarr radarr lidarr readarr bazarr prowlarr SonarrUsers RadarrUsers", "global", "en", "tech")
B("transmission_bt qbittorrent rtorrent deluge utorrent transmission_news", "global", "en", "tech")
B("portainer portainer_news watchtower_news containrrr coolify coolify_io dockge", "global", "en", "tech")
B("nextcloud nextcloud_news owncloud owncloud_news syncthing syncthing_official rclone_news rclone", "global", "en", "tech")
B("pihole pihole_official adguard adguard_official adguardhome wireguard wireguard_news tailscale tailscale_news", "global", "en", "tech")
B("immich_app immich photoprism photoprism_news fileshare_news fileshare_official", "global", "en", "tech")
B("casaos casaos_official", "global", "en", "tech")

# ════════════════════════════════════════════════════════════════════
# MISCELLANEOUS HIGH-CONFIDENCE
# ════════════════════════════════════════════════════════════════════
B("wikipedia wikipedia_org wikipedia_news wikileaks wikileaks_news wikimediafoundation", "global", "en", "news")
B("snowdenofficial julian_assange the_intercept_news privacy_news privacyguides privacyguides_official", "global", "en", "news")
B("eff_news eff_org electronic_frontier_foundation privacy_international gnu_org fsf_news fsf_official", "global", "en", "tech")
B("ietf_news ietf_official rfc_editor w3c_news w3c_official", "global", "en", "tech")
B("apple apple_news appleofficial google google_news googleofficial microsoft microsoftnews microsoft_official", "global", "en", "tech")
B("samsung samsung_news samsungofficial sony sony_news sonyofficial xiaomi xiaomi_news xiaomiofficial huawei huawei_news", "global", "en", "tech")
B("alphabet alphabetinc meta meta_news facebook_news meta_official tiktok tiktok_news tiktokofficial", "global", "en", "tech")

# ════════════════════════════════════════════════════════════════════
# ADDITIONAL PATTERN-DERIVED CHANNELS (genre-conventional names)
# These are guesses based on common Telegram channel naming conventions.
# Many will be `dead` after health check, but pattern coverage helps
# surface real channels we may have missed.
# ════════════════════════════════════════════════════════════════════
# Movies — Hindi/Indian pattern variations
B("hindi_dubbed_movies hindi_dubbed_films hindidubbed hindi_dubbed_collection hindi_dubbed_world", "in", "hi", "cinema")
B("hindi_movies_2025 hindi_movies_2024 hindi_movies_2023 hindi_movies_pro hindi_movies_unlimited hindi_movie_world", "in", "hi", "cinema")
B("hd_hindi_movies hindi_movies_full hindi_movies_master hindi_movies_collection hindi_movies_treasure", "in", "hi", "cinema")
B("bollywood_collection bollywood_treasure bollywood_archive bollywood_world_hub bollywood_pro", "in", "hi", "cinema")
B("south_indian_movies south_movies south_dubbed_hindi southmovies_hindi south_indian_hindi south_hindi_movies", "in", "hi", "cinema")
B("tamil_movies_2025 tamil_movies_2024 tamil_movies_full tamil_movies_treasure tamil_movies_pro tamil_movies_master", "in", "ta", "cinema")
B("telugu_movies_2025 telugu_movies_2024 telugu_movies_full telugu_movies_treasure telugu_movies_pro", "in", "te", "cinema")
B("malayalam_movies_2025 malayalam_movies_full malayalam_movies_pro malayalam_movies_treasure", "in", "ml", "cinema")
B("kannada_movies_2025 kannada_movies_pro kannada_movies_treasure", "in", "kn", "cinema")
B("bengali_movies_pro bengali_movies_world", "in", "bn", "cinema")
B("punjabi_movies_pro punjabi_movies_world punjabi_movies_collection", "in", "pa", "cinema")

# Movies — Hollywood/English pattern variations
B("hollywood_movies_2025 hollywood_movies_2024 hollywood_movies_full hollywood_movies_master hollywood_collection", "global", "en", "cinema")
B("english_movies_hd english_movies_world english_movies_pro english_movies_full", "global", "en", "cinema")
B("hd_movies_pro hd_movies_full hd_movies_world hd_movies_master HD_movies_collection", "global", "en", "cinema")
B("hollywood_4k hollywood_uhd hollywood_2160p hollywood_dolby hollywood_imax", "global", "en", "cinema")
B("foreign_films foreign_movies foreignfilm_world world_cinema_world world_cinema_pro", "global", "en", "cinema")
B("classic_movies classic_films old_movies old_films classic_cinema cinemaclassics", "global", "en", "cinema")
B("indie_movies indie_films independent_cinema indie_film_world art_house_films arthouse", "global", "en", "cinema")
B("documentary_films documentaries documentary_world doc_films doc_world bestdocumentaries", "global", "en", "cinema")
B("horror_movies horror_films horror_world horrorfilms_world scary_movies", "global", "en", "cinema")
B("scifi_movies scifi_films science_fiction_movies sci_fi_films", "global", "en", "cinema")
B("action_movies action_films action_movie_world action_collection action_pro", "global", "en", "cinema")
B("comedy_movies comedy_films comedy_movie_world comedy_collection comedy_pro", "global", "en", "cinema")
B("romance_movies romance_films romance_collection romcom_world", "global", "en", "cinema")
B("animated_movies animation_films animated_world animation_hub disney_animated pixar_collection", "global", "en", "cinema")

# TV series patterns
B("netflix_series netflix_shows netflix_collection netflix_archive netflix_world", "global", "en", "cinema")
B("hbo_series hbo_shows hbo_archive hbo_world hbo_collection", "global", "en", "cinema")
B("amazon_series amazon_shows prime_video_series amazonprime_series", "global", "en", "cinema")
B("disney_series disney_shows disney_plus_series disneyplus_shows", "global", "en", "cinema")
B("series_2025 series_2024 series_world series_pro series_collection series_archive", "global", "en", "cinema")
B("tvshows_world tvshows_pro tvshows_collection tvshows_archive tvshows_2025 tvshows_2024", "global", "en", "cinema")
B("kdrama_world kdrama_pro kdrama_collection kdrama_archive k_drama_world korean_drama", "kr", "ko", "cinema")
B("turkish_drama_world turkish_dizi_world turkishseries_world turkishdrama_pro", "tr", "tr", "cinema")

# Music pattern variations
B("english_music_pro english_music_world english_songs_world", "global", "en", "music")
B("hindi_songs hindi_music_world hindi_music_pro hindi_music_collection bollywood_songs", "in", "hi", "music")
B("tamil_songs tamil_music_world tamil_music_pro tamil_music_collection", "in", "ta", "music")
B("telugu_songs telugu_music_world telugu_music_pro telugu_music_collection", "in", "te", "music")
B("punjabi_songs punjabi_music_world punjabi_music_pro", "in", "pa", "music")
B("arabic_songs arabic_music_pro arabic_music_world arabic_songs_hub", "mena", "ar", "music")
B("kpop_songs kpop_music_pro kpop_world kpop_collection", "global", "en", "music")
B("persian_songs persian_music_pro persian_music_world farsi_music persian_songs_hub", "ir", "fa", "music")
B("turkish_songs turkish_music_pro turkish_music_world", "tr", "tr", "music")

# Books pattern variations
B("english_books_pdf english_ebooks_pdf english_books_collection english_books_pro english_books_world", "global", "en", "ebook")
B("hindi_books hindi_books_pdf hindi_ebooks hindi_books_world hindi_books_pro", "in", "hi", "ebook")
B("urdu_books_pdf urdu_books_world urdu_ebooks", "pk", "ur", "ebook")
B("arabic_books_pdf arabic_books_world arabic_ebooks", "mena", "ar", "ebook")
B("persian_books_pdf farsi_books_pdf persian_books_world farsi_ebooks", "ir", "fa", "ebook")
B("russian_books_pdf russian_ebooks_pdf russian_books_world", "ru", "ru", "ebook")
B("chinese_books_pdf chinese_ebooks_pdf chinese_books_world", "cn", "zh", "ebook")
B("free_books free_ebooks_world free_books_world books_free books_free_pdf", "global", "en", "ebook")

# Software / utilities patterns (likely sketchy but training-data common)
B("software software_world apps_world apps_collection apps_pro pcmag windows_software", "global", "en", "tech")
B("mac_apps mac_software macos_apps macos_software", "global", "en", "tech")
B("android_apps android_apps_world android_apps_pro android_apps_premium android_apps_modded", "global", "en", "tech")
B("ios_apps ios_apps_world ios_apps_pro", "global", "en", "tech")
B("graphics_world graphics_pro graphics_collection graphics_resources", "global", "en", "tech")
B("templates_world templates_pro templates_collection template_world", "global", "en", "tech")
B("courses_collection courses_world courses_pro courses_premium courses_master courses_archive", "global", "en", "education")
B("udemy_courses_free udemy_free udemy_premium udemy_paid_free", "global", "en", "education")
B("coursera_free coursera_premium coursera_paid_free edx_free edx_premium", "global", "en", "education")
B("freecourses freecourses_world freecourses_pro free_courses_premium", "global", "en", "education")
B("english_learning english_speaking english_grammar english_vocab english_practice", "global", "en", "education")
B("ielts ielts_preparation ielts_speaking ielts_writing ielts_listening", "global", "en", "education")
B("toefl toefl_preparation toefl_speaking toefl_practice", "global", "en", "education")
B("gre_preparation gre_quant gre_verbal gmat gmat_preparation cat_preparation", "global", "en", "education")

# Sports patterns
B("football_news football_world football_pro footballworld_news soccer_news soccerworld", "global", "en", "sport")
B("cricket_news cricket_world cricket_pro cricket_live_news", "in", "en", "sport")
B("basketball_world basketball_news basketball_pro nba_world nba_pro", "global", "en", "sport")
B("tennis_world tennis_news tennis_pro", "global", "en", "sport")
B("formula1_news f1_world f1_pro motorsport_news", "global", "en", "sport")

# News patterns by language
B("english_news english_news_world world_news_english worldnews_today", "global", "en", "news")
B("breaking_news_world breakingnews24 worldnews_breaking globalnewstoday", "global", "en", "news")
B("trending_news_world trending_world trending_today daily_news_today", "global", "en", "news")
B("hindi_news_world hindi_news_today hindi_breaking_news hindi_news_pro", "in", "hi", "news")
B("urdu_news_world urdu_news_today urdu_breaking_news urdu_news_pro", "pk", "ur", "news")
B("arabic_news_world arabic_news_today arabic_breaking_news", "mena", "ar", "news")
B("chinese_news_world chinese_news_today chinese_breaking_news cn_news_today", "cn", "zh", "news")
B("persian_news_world persian_news_today farsi_news_world farsi_news_today", "ir", "fa", "news")
B("turkish_news_world turkish_news_today tr_news_today", "tr", "tr", "news")
B("russian_news_world russian_news_today russia_news_today russia_breaking", "ru", "ru", "news")
B("spanish_news_world spanish_news_today es_news_today", "es", "es", "news")
B("french_news_world french_news_today fr_news_today", "fr", "fr", "news")
B("german_news_world german_news_today de_news_today", "de", "de", "news")
B("italian_news_world italian_news_today it_news_today", "it", "it", "news")
B("portuguese_news_world portuguese_news_today pt_news_today brazil_news_today", "br", "pt", "news")

# Tech news patterns
B("tech_news_world tech_news_today tech_news_pro tech_news_archive tech_world_news", "global", "en", "tech")
B("ai_news_world ai_news_today ai_news_pro ai_news_archive", "global", "en", "tech")
B("programming_news programming_world programming_pro programming_today", "global", "en", "tech")
B("startup_news startup_world startups_today startupsofficial", "global", "en", "tech")
B("entrepreneur entrepreneur_news entrepreneur_world entrepreneur_pro", "global", "en", "business")

# Memes patterns
B("memes_world memes_pro memes_today memes_archive memes_collection", "global", "en", "humor", media="photo,video")
B("funny_memes funny_videos funny_world funny_today funny_collection", "global", "en", "humor", media="photo,video")
B("dank_memes_world dankest_memes dank_humor", "global", "en", "humor", media="photo,video")

# Travel patterns
B("travel_pics travel_photos travel_collection travel_pro travel_archive", "global", "en", "travel", media="photo")
B("travel_videos travel_vlogs travel_youtube travel_blog travel_blog_world", "global", "en", "travel", media="video")
B("destination_world destinations_pro destinations_collection destination_today", "global", "en", "travel")

# Health/fitness patterns
B("fitness_world fitness_pro fitness_today fitness_archive fitness_news_world", "global", "en", "health")
B("yoga_world yoga_pro yoga_today yoga_archive yoga_news_world", "global", "en", "health")
B("nutrition_world nutrition_pro nutrition_today nutrition_news", "global", "en", "health")
B("workout_world workout_pro workout_today workout_archive", "global", "en", "health")

# Quotes / inspiration patterns
B("quotes quotes_world quotes_pro quotes_today quotes_archive quotes_collection daily_quotes", "global", "en", "quotes")
B("motivation motivation_world motivation_pro motivation_today motivation_archive motivation_daily", "global", "en", "quotes")
B("inspiration_world inspiration_pro inspiration_today inspiration_daily", "global", "en", "quotes")
B("success_stories success_world success_pro success_today", "global", "en", "quotes")
B("life_quotes lifequotes life_advice lifeadvice life_lessons", "global", "en", "quotes")

# Wallpapers / mobile content patterns
B("wallpapers_4k wallpapers_hd wallpapers_world wallpapers_pro wallpapers_collection mobile_wallpapers iphone_wallpapers android_wallpapers", "global", "en", "art", media="photo")
B("ringtones ringtones_world ringtones_collection ringtones_pro ringtones_archive", "global", "en", "music", media="audio")

# Photography patterns
B("photography_world photography_pro photography_collection photography_archive photography_today", "global", "en", "art", media="photo")
B("nature_photography nature_photos nature_world nature_pro animal_photos wildlife_photos", "global", "en", "art", media="photo")

# Science patterns
B("science_world science_news_world science_pro science_today science_archive", "global", "en", "science")
B("physics_world physics_news physics_pro biology_world biology_news biology_pro chemistry_world chemistry_news", "global", "en", "science")
B("space_news space_world space_pro space_today astronomy_world astronomy_pro", "global", "en", "science")

# Gaming patterns
B("gaming_news_world gaming_news_today gaming_pro gaming_archive gaming_collection", "global", "en", "game")
B("game_leaks_world gameleaks_today gameleaks_archive gamerumors_world", "global", "en", "game")

# Crypto patterns
B("crypto_news_world crypto_news_today crypto_news_pro crypto_news_archive crypto_today crypto_pro", "global", "en", "crypto")
B("bitcoin_news_world bitcoin_world bitcoin_pro bitcoin_today bitcoin_archive", "global", "en", "crypto")
B("ethereum_news_world ethereum_world ethereum_pro eth_news_world", "global", "en", "crypto")
B("nft_world nft_pro nft_news_world nft_today nft_archive", "global", "en", "crypto")
B("defi_world defi_pro defi_news_world defi_today defi_archive", "global", "en", "crypto")
B("altcoin_news altcoin_world altcoin_pro altcoin_today altcoin_archive", "global", "en", "crypto")
B("crypto_pump crypto_pumps cryptopump_signals pumps_signals cryptopumpgroup", "global", "en", "crypto")
B("crypto_signals_premium crypto_signals_pro cryptosignals_world cryptosignal_world crypto_signals_archive", "global", "en", "crypto")
B("crypto_calls cryptocalls crypto_calls_pro crypto_alpha cryptoalpha_world", "global", "en", "crypto")

# Anime patterns
B("anime_world anime_pro anime_today anime_archive anime_collection", "global", "en", "anime")
B("anime_news_world anime_news_today anime_pro_news", "global", "en", "anime")
B("anime_dubbed_world anime_subbed_world anime_eng_subbed anime_eng_dubbed", "global", "en", "anime")
B("manga_world manga_pro manga_today manga_archive manga_collection", "global", "en", "anime")

# Fashion / style patterns
B("fashion_world fashion_pro fashion_today fashion_collection style_world style_pro", "global", "en", "fashion", media="photo")
B("streetstyle streetfashion streetwear streetwear_world streetwear_pro", "global", "en", "fashion", media="photo")
B("luxurystyle luxury_fashion luxury_world luxury_brands", "global", "en", "fashion")

# Food / cooking patterns
B("food_world food_pro food_today food_collection cooking_world cooking_pro", "global", "en", "food")
B("recipes_world recipes_pro recipes_today recipes_collection recipes_archive", "global", "en", "food")
B("vegan_world vegan_pro vegan_news plantbased_world plantbased_pro", "global", "en", "food")

# Cars / auto patterns
B("cars_world cars_pro cars_today cars_archive cars_collection", "global", "en", "auto", media="photo,video")
B("luxury_cars luxury_cars_world luxury_cars_pro supercars supercars_world supercars_pro", "global", "en", "auto", media="photo,video")
B("electric_cars_world ev_world ev_pro ev_today", "global", "en", "auto")

# Iranian extra
B("iran_news iran_news_world iran_news_today iran_breaking iran_pro_news", "ir", "fa", "news")
B("iran_music iran_music_world iran_music_pro iran_songs iranian_songs", "ir", "fa", "music")
B("iran_movies iran_movies_world iran_movies_pro iran_cinema_world", "ir", "fa", "cinema")
B("iran_books iran_books_world iran_books_pro iran_books_pdf", "ir", "fa", "ebook")
B("iran_education iran_courses iran_learning farsi_learning_world", "ir", "fa", "education")

# Russian extra
B("ru_news ru_pro ru_archive ru_collection ru_world", "ru", "ru", "news")
B("ru_music_world ru_music_pro ru_songs russian_songs_world", "ru", "ru", "music")
B("ru_cinema ru_films ru_movies_world ru_movies_pro russian_films_world", "ru", "ru", "cinema")
B("ru_books_world ru_books_pro russian_books_world russian_ebooks_world", "ru", "ru", "ebook")
B("ru_courses ru_education ru_learning russian_courses_world", "ru", "ru", "education")
B("ru_gaming ru_games gaming_ru russian_gaming russian_games_world", "ru", "ru", "game")
B("ru_crypto crypto_russian ru_bitcoin ru_eth ru_blockchain", "ru", "ru", "crypto")
B("ru_tech tech_russian ru_programming ru_dev russian_dev_world", "ru", "ru", "tech")

# China extra
B("china_pro china_archive china_news_world china_collection china_today", "cn", "zh", "news")
B("cn_music cn_movies cn_books cn_courses cn_tech cn_gaming cn_crypto cn_anime", "cn", "zh", "mixed")

# India extra
B("india_news india_pro india_archive india_news_world india_today_news india_collection", "in", "en", "news")
B("india_music india_music_world india_music_pro indianmusic_world", "in", "en", "music")
B("india_cinema india_films india_movies_world india_movies_pro indian_films_world", "in", "en", "cinema")
B("india_education india_learning india_courses indian_courses_world", "in", "en", "education")
B("india_tech tech_indian indian_dev indian_programming indian_tech_world", "in", "en", "tech")
B("india_gaming indian_gaming indian_games_world", "in", "en", "game")
B("india_crypto indian_crypto_pro india_bitcoin india_eth", "in", "en", "crypto")

# ════════════════════════════════════════════════════════════════════
# INDONESIA — huge TG market
# ════════════════════════════════════════════════════════════════════
B("detikcom detik_news kompas_com kompas tempodotco tempo_co cnnindonesia liputan6 okezone merdekadotcom", "id", "id", "news")
B("republikaonline tribunnews antaranews antara_news kontannews bisnisindonesia katadata jakartaglobe thejakartapost", "id", "id", "news")
B("kumparancom kumparan jpnncom tribunjakarta tribuntimur tribunpontianak detikfinance detikinet kompastechno", "id", "id", "news")
B("kompasinternasional kompastv tvonenews tvone tvone_news metrotv metrotv_news inews", "id", "id", "news")
B("bbcindonesia voaindonesia voa_indonesia dwindonesia dw_indonesia france24_id rt_indonesia", "id", "id", "news")
B("indomoviemania indofilm indofilm_hd indo_film indolayar indolayar_hd film_indonesia film_indo", "id", "id", "cinema")
B("filmbioskopindo film_indonesia_pro film_bioskop_id film_box_office_id indomovies_hd", "id", "id", "cinema")
B("indonesia_music musik_indonesia indo_music indolagu lagu_indonesia musisi_indonesia", "id", "id", "music")
B("ebookindonesia ebook_indonesia buku_indonesia pdf_indonesia perpus_indonesia", "id", "id", "ebook")
B("kuliner_indonesia masakan_indonesia resep_indonesia resep_masakan", "id", "id", "food")
B("wisata_indonesia traveling_indonesia explorewonderfulindonesia", "id", "id", "travel")
B("pendidikan_indonesia kelas_pintar ruangguru_official zenius_official quipper_id", "id", "id", "education")
B("tech_indonesia teknologi_id tekno_indonesia gadget_indonesia indotekno", "id", "id", "tech")
B("crypto_indonesia indodax indodax_news bitocto_news pintu_news", "id", "id", "crypto")
B("sepakbola_indonesia bolasport pssi_indonesia liga1 liga_indonesia bola_indonesia bolaindonesia", "id", "id", "sport")
B("startup_indonesia startup_id wirausaha_indonesia entrepreneur_indonesia", "id", "id", "business")

# ════════════════════════════════════════════════════════════════════
# VIETNAM
# ════════════════════════════════════════════════════════════════════
B("vnexpress vnexpressnet thanhnien thanhnien_news tuoitre tuoitrenews vietnamnet vietnam_net zingnewsvn", "vn", "vi", "news")
B("vietnamplus vov_vn nhandan_vn dantri laodong tienphong cafef cafef_vn cafebiz", "vn", "vi", "news")
B("bbcvietnamese bbc_vietnamese voavietnamese voa_vietnamese dwvietnam dw_vietnamese rfa_vietnamese", "vn", "vi", "news")
B("phimviet phim_viet vietnam_movies vn_movies phim_hay phim_le phim_bo phimchieurap_vn", "vn", "vi", "cinema")
B("vpop vpop_news nhac_viet nhac_vn vietnam_music vmusic vn_songs", "vn", "vi", "music")
B("crypto_vietnam vietnam_crypto crypto_vn coin68_vn coin98_vn", "vn", "vi", "crypto")
B("vietnam_tech tech_vietnam genk_vn techsignin_vn ictnews_vn", "vn", "vi", "tech")
B("vleague_vn bongda_vn vnf_news bongda_news", "vn", "vi", "sport")
B("sach_viet sach_vietnam vn_ebooks vietnam_books vn_pdf_books", "vn", "vi", "ebook")

# ════════════════════════════════════════════════════════════════════
# THAILAND
# ════════════════════════════════════════════════════════════════════
B("bangkokpost bangkok_post thairath thairathnews thaipbs thaipbsnews matichon dailynews_thai khaosod", "th", "th", "news")
B("sanook sanook_news pptvhd36 workpoint_news kapook kapookcom thaipost mgronline manager_online", "th", "th", "news")
B("bbcthai voathai dwthai france24_thai rfa_thai", "th", "th", "news")
B("thai_movies movies_thai thai_film thaifilm thai_films thaiseries_world thaidrama thaidrama_world", "th", "th", "cinema")
B("tpop thaipop thai_music thaimusic_world thai_songs thaisongs thaihits", "th", "th", "music")
B("thai_books thaibook thaibooks_world thai_pdf_books thai_ebooks", "th", "th", "ebook")
B("thaifootball football_thai thai_premier_league mu_th lfc_th", "th", "th", "sport")

# ════════════════════════════════════════════════════════════════════
# PHILIPPINES — large TG user base
# ════════════════════════════════════════════════════════════════════
B("inquirer_news inquirerdotnet rappler abscbnnews gma_news gma_network mb_com manilabulletin", "ph", "en", "news")
B("philstarnews philstar_com sunstar_news cnn_philippines manilatimes_today themanilatimes", "ph", "en", "news")
B("mothershipsg mothership", "sg", "en", "news")
B("ph_movies filipinomovies filipino_movies pinoymovies pinoy_movies pinoybox pinoy_box_office", "ph", "tl", "cinema")
B("tagalog_movies tagalogmovies pinoyteleserye pinoy_teleserye abscbn_dramas gma_dramas", "ph", "tl", "cinema")
B("opm_official opm_music opmusic pinoymusic_world pinoymusic filipino_music filipinomusic", "ph", "tl", "music")
B("pba_basketball pba_news basketballph azkals_philippines philippine_basketball", "ph", "en", "sport")
B("filipinoauthors filipinobooks filipino_books pinoybooks tagalog_books", "ph", "tl", "ebook")

# ════════════════════════════════════════════════════════════════════
# MALAYSIA / SINGAPORE / BRUNEI
# ════════════════════════════════════════════════════════════════════
B("thestar_my thestarmalaysia malaysiakini malaymailonline malaymail bernama_my bernama_news", "my", "ms", "news")
B("astroawani astro_awani sinarharian harianmetro berita_harian beritaharian utusan utusan_news", "my", "ms", "news")
B("malaysia_news malaysianews_world freemalaysiatoday fmtnews fmt_news", "my", "ms", "news")
B("malayfilm malayfilms malay_movies malaysian_movies malayan_cinema melayu_film", "my", "ms", "cinema")
B("malay_music musikmalaysia malaymusic_world", "my", "ms", "music")

B("straitstimes channelnewsasia CNA_today CNA_news mothership todayonline today_online stcomonline", "sg", "en", "news")

# ════════════════════════════════════════════════════════════════════
# MYANMAR / CAMBODIA / LAOS / MONGOLIA
# ════════════════════════════════════════════════════════════════════
B("bbcburmese voa_burmese dw_burmese rfa_burmese myanmar_now myanmarnow frontiermyanmar mizzima_news irrawaddy", "mm", "my", "news")
B("cambodia_daily voa_khmer rfa_khmer bbc_khmer phnompenh_post phnompenhpost", "kh", "km", "news")
B("vientianetimes laodian_post bbc_lao voa_lao", "la", "lo", "news")
B("mongolia_news montsame mongolnews ub_post", "mn", "mn", "news")

# ════════════════════════════════════════════════════════════════════
# AFRICA — generally underserved
# ════════════════════════════════════════════════════════════════════
B("africanews africanews_english allafrica africanews_world africa_news bbcafrica bbc_africa bbcafrica_news", "africa", "en", "news")
B("voa_africa voanews_africa dw_africa dwafrica france24_africa rfi_afrique", "africa", "en", "news")

# Nigeria
B("punchng punch_ng premiumtimesng premiumtimes thecableng thecable_ng vanguardngr vanguard_news guardianng", "ng", "en", "news")
B("dailytrust daily_trust thisdayng nationng dailypostng tribuneng saharareporters tribuneonlineng", "ng", "en", "news")
B("naijaloaded notjustok pulsenigeria pulse_nigeria bellanaija bella_naija lindaikejiblog lindaikeji", "ng", "en", "news")
B("nollywood nollywood_movies nollywoodnews nollywood_official nigerianmovies", "ng", "en", "cinema")
B("afrobeats afrobeatsworld afrobeatsofficial naijamusic naija_music nigerian_music wizkidofficial burnaboy davidoofficial", "ng", "en", "music")

# Kenya
B("citizentvkenya citizen_tv ntvkenya ntv_kenya k24tvkenya k24_tv kbctvkenya kbc_tv", "ke", "en", "news")
B("nationafrica nationmediaafrica standardkenya standard_kenya thestar_ke", "ke", "en", "news")
B("kenya_news kenya_news_world kenyans_co_ke tuko_news", "ke", "en", "news")

# South Africa
B("news24 news_24 ewn ewn_news enca_news enca timeslive sundayindependent_za", "za", "en", "news")
B("citypress mailandguardian sowetan citizen_za thedailyvox", "za", "en", "news")
B("safmradio polity_org_za", "za", "en", "news")

# Ghana
B("ghana_web graphic_dotcom dailygraphic graphicghana myjoyonline citinewsroom", "gh", "en", "news")

# Ethiopia
B("ethiopianreporter addisstandard borkena fanabc fana_bc waltatv", "et", "am", "news")

# ════════════════════════════════════════════════════════════════════
# MENA — EXTENDED
# ════════════════════════════════════════════════════════════════════
# Egypt deeper
B("youm7 youm7news masrawy masrawy_news dot_msr almasryalyoum_official ahram_news shorouk_news shorouknews", "eg", "ar", "news")
B("cairo24 cbcegypt mbcmasr egypt_today_news egyptindependent egypt_independent egypt_today", "eg", "ar", "news")
# Saudi
B("alriyadh alriyadh_news okaz okaz_news arabnews_official saudigazette saudi_gazette spa_news_saudi", "sa", "ar", "news")
B("alarabiya_official sabq_news alyaum alyaum_news makkahnews", "sa", "ar", "news")
# UAE
B("khaleejtimes_news the_national_uae thenationaluae emaratalyoum gulfnews_uae", "ae", "ar", "news")
B("dubaipolice dubaimedia rta_dubai abudhabi_official abudhabi_news dubaitvchannel", "ae", "ar", "news")
# Iraq
B("alsumarianews alsumaria_news alforatnews al_furat_news shafaaq shafaaqnews almadanews al_mada", "iq", "ar", "news")
# Lebanon
B("lbcgroup lbci_news lbci_official mtv_lebanon mtvlebanon_news annahar_lb annahar almanar_news", "lb", "ar", "news")
B("almayadeen almayadeen_official almayadeen_news daraj_media", "lb", "ar", "news")
# Jordan
B("jordan_times jordantimes alghad_news alghad_jo addustour_jo addustour roya_news roya_jordan", "jo", "ar", "news")
# Kuwait/Qatar/Bahrain/Oman
B("alqabas_kuwait alrai_kuwait kuwait_news_world", "kw", "ar", "news")
B("aljazeera_qatar aljazeerachannel aljazeera_news gulf_times peninsulaqatar peninsulaqatar_news qna_qatar", "qa", "ar", "news")
B("alwasatnews bahrainnews akhbar_alkhaleej", "bh", "ar", "news")
B("alwatan_oman omantribune timesofoman observerom", "om", "ar", "news")
# Morocco/Algeria/Tunisia
B("hespress hespress_news lematin_ma lemonde_maroc_official le_matin yabiladi medi1tv medi1news", "ma", "ar", "news")
B("ennaharonline ennahartv echoroukonline echorouk algeria_news el_watan elwatan_dz", "dz", "ar", "news")
B("mosaiquefm mosaique_fm tunisie_telegraph tunisienumerique businessnews_tn", "tn", "ar", "news")
# Libya/Sudan/Yemen
B("libyaobserver libyaherald libya_news_world libya_24", "ly", "ar", "news")
B("sudan_news sudan_tribune sudanesedotcom altaghyeer", "sd", "ar", "news")
B("alarabiyatv_yemen yemen_news_world saba_news mareb_press", "ye", "ar", "news")

# Persian/Iran extra
B("akhbar_iran iran_news_now iran_pro_news iran_pro_world iran_today_news iran_breaking_news", "ir", "fa", "news")
B("filmnet_iran filmnet_official filmnet_filmha", "ir", "fa", "cinema")
B("namava_iran namava_official namava_filmha", "ir", "fa", "cinema")
B("hamshahri_official hamshahri_news_official aftabnews aftab_news afkarnews", "ir", "fa", "news")

# Israel/Hebrew
B("haaretzcom haaretzofficial ynet ynet_news kanchannel11 kan11 makorrishon news13_israel news12_israel", "il", "he", "news")
B("walla walla_news mako mako_news n12 n12_news ice_israel calcalist globes_israel", "il", "he", "news")
B("jerusalempost timesofisrael israelnationalnews israel_national_news israel_today israeltoday", "il", "en", "news")

# ════════════════════════════════════════════════════════════════════
# EUROPE — EXTENDED (countries previously thin)
# ════════════════════════════════════════════════════════════════════
# Greece
B("kathimerini kathimerini_official toveio newstv_gr proto_thema protothema athens_voice", "gr", "el", "news")
B("ert_gr ert_news skai_gr skai_news in_gr cnn_gr cnngreece", "gr", "el", "news")
# Hungary/Poland (extra)
B("origo_hu hvg_hu telex_hu 444_hu_news 24_hu mandiner_hu", "hu", "hu", "news")
B("polsatnews tvn24 tvnews_pl onet_news onet_pl interia_pl wp_pl rmf24 rmf_fm rzeczpospolita", "pl", "pl", "news")
# Romania/Bulgaria/Serbia
B("digi24 stirileprotv adevarul g4media hotnews_ro mediafax_ro pro_tv antena3 antena1 jurnalul_national", "ro", "ro", "news")
B("dnevnik_bg vesti_bg bnr_news novini bnt_news bnt_official 24chasa", "bg", "bg", "news")
B("rts_news rts_official b92_news n1info_serbia n1_serbia danas_rs novosti_rs blic", "rs", "sr", "news")
B("informer_rs telegraf_rs nova_rs", "rs", "sr", "news")
# Czech/Slovakia
B("idnes_news idnes ihned ihnedcz aktualne_cz aktualnecz seznamzpravy ct24 zpravy_ct", "cz", "cs", "news")
B("sme_sk pravda_sk dennikn_sk denniknnews aktuality_sk", "sk", "sk", "news")
# Croatia/Slovenia/Bosnia/Albania
B("hrt_hr index_hr 24sata jutarnji_hr vecernji_hr nacional_hr", "hr", "hr", "news")
B("rtvslo delo_si vecer_si dnevnik_si", "si", "sl", "news")
B("klix_ba avaz_ba radiosarajevo dnevni_avaz", "ba", "bs", "news")
B("balkanweb top_channel_al klan_kosova kanali10_al lapsi_news", "al", "sq", "news")
B("ekathimerini gazeta_express", "al", "sq", "news")
# Baltics
B("delfi_lt delfi_lv delfi_ee lrt_lt err_ee err_news lsm_lv tvnet_lv lrytas_lt 15min_lt", "lt", "lt", "news")
B("postimees ohtuleht eesti_paevaleht err_estonia", "ee", "et", "news")

# ════════════════════════════════════════════════════════════════════
# CENTRAL ASIA
# ════════════════════════════════════════════════════════════════════
B("nur_kz tengrinews tengri_news kursiv_kz informburo zakon_kz forbes_kz totalkz", "kz", "kk", "news")
B("kloop kloop_kg azattyk_kg radio_azattyk 24kg akipress kaktus_media", "kg", "ky", "news")
B("asia_plus_tj sputnik_tj radio_azadi_tajikistan", "tj", "tg", "news")
B("turkmenistan_news rferl_turkmen", "tm", "tk", "news")
B("podrobno_uz gazeta_uz upl_uz daryo_uz kun_uz xabar_uz", "uz", "uz", "news")
B("bbcazeri bbcrussian rferl_russian sputnik_azerbaijan", "az", "az", "news")
B("armenia_news armtimes a1plus 1in_am hetq_am armenpress factor_news_am", "am", "hy", "news")
B("georgia_news 1tv_ge interpressnews netgazeti civil_ge agenda_ge", "ge", "ka", "news")

# ════════════════════════════════════════════════════════════════════
# SOUTH KOREA / JAPAN — extra (TG smaller but exists)
# ════════════════════════════════════════════════════════════════════
B("yna_news yonhap_news yonhap_korean koreajoongang koreajoongangdaily koreaherald koreatimes hani_co", "kr", "ko", "news")
B("kbs_news mbc_news sbs_news jtbc_news ohmynews pressian_news yon_hap", "kr", "ko", "news")
B("nikkei_jp asahi_shimbun yomiuri mainichi_jp japantimes nhk_japan nhk_news", "jp", "ja", "news")
B("japanvisitor_jp tokyoreporter japaninfo_jp jpopasia jrock_world j_pop_world", "jp", "ja", "music")

# ════════════════════════════════════════════════════════════════════
# Bangladesh / Sri Lanka / Nepal
# ════════════════════════════════════════════════════════════════════
B("prothomalo prothom_alo daily_star_bd dailystar bdnews24 channeli_news jamuna_tv ntv_bd", "bd", "bn", "news")
B("bbcbangla bbc_bangla voa_bangla voa_bengali dw_bangla", "bd", "bn", "news")
B("dailymirror_lk ada_derana lankaweb sundaytimeslk newsfirstlk", "lk", "si", "news")
B("kantipur_news ekantipur online_khabar himalkhabar setopati", "np", "ne", "news")

# ════════════════════════════════════════════════════════════════════
# MEXICO / LATAM EXTRA
# ════════════════════════════════════════════════════════════════════
B("mexico_news_world milenio_news milenio mexico_today reforma_news aristegui_news el_universal_mx eluniversal", "mx", "es", "news")
B("animalpolitico animal_politico animal_political", "mx", "es", "news")
B("clarin_ar lanacionarg lanacion_ar perfil_arg pagina12 cronica_ar tn_argentina infobaeworld", "ar", "es", "news")
B("emol_chile latercerachile cooperativa_cl biobio_cl 24horas_cl", "cl", "es", "news")
B("eltiempo_co semana_news caracoltv noticiascaracol citytv_co rcn_co", "co", "es", "news")
B("granmainternacional cubanews bohemia_cu", "cu", "es", "news")
B("eltiempove ultimasnoticias_ve diario_panorama_ve", "ve", "es", "news")

# ════════════════════════════════════════════════════════════════════
# Output
def _emit():
    seen, dedup = set(), []
    for e in E:
        k = e["username"].lower()
        if k in seen:
            continue
        seen.add(k)
        dedup.append(e)
    out = {
        "version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_note": "Telegram channels recalled from training data. All `unverified` pending health-check.",
        "total": len(dedup),
        "channels": dedup,
    }
    yaml.safe_dump(out, sys.stdout, sort_keys=False, allow_unicode=True)
    print(f"# total entries before dedup: {len(E)}", file=sys.stderr)
    print(f"# unique after dedup:         {len(dedup)}", file=sys.stderr)


if __name__ == "__main__":
    _emit()
