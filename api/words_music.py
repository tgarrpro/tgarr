"""The words of the Music page: the track list, its chips and sort controls, the audiobook
shelf, the player at the bottom and the now-playing panel. The same shape as T in i18n.py."""

# English: (zh-Hans, zh-Hant, es, fr, de, pt, ru, ja, ko, he, ar)
T = {
    # ── the shelf and its chips ──
    "In order": ("按顺序", "按順序", "En orden", "Dans l'ordre", "Der Reihe nach", "Em ordem", "По порядку", "順番どおり", "순서대로", "לפי הסדר", "بالترتيب"),
    "Longest": ("最长", "最長", "Más largos", "Les plus longs", "Längste", "Mais longos", "Самые длинные", "長い順", "긴 순", "הארוכים ביותר", "الأطول"),
    "back to the songs": ("回到歌曲", "回到歌曲", "volver a las canciones", "retour aux chansons", "zurück zu den Liedern", "voltar às músicas", "назад к песням", "曲に戻る", "노래로 돌아가기", "חזרה לשירים", "العودة إلى الأغاني"),
    # ── the list ──
    "title": ("标题", "標題", "título", "titre", "Titel", "título", "название", "タイトル", "제목", "כותרת", "العنوان"),
    "duration": ("时长", "時長", "duración", "durée", "Dauer", "duração", "длительность", "長さ", "길이", "משך", "المدة"),
    "posted": ("发布日期", "發布日期", "publicado", "publié", "gepostet", "publicado", "опубликовано", "投稿日", "게시일", "פורסם", "تاريخ النشر"),
    "favourite": ("收藏", "收藏", "favorito", "favori", "Favorit", "favorito", "в избранное", "お気に入り", "즐겨찾기", "מועדף", "مفضلة"),
    "download this file": ("下载此文件", "下載此檔案", "descargar este archivo", "télécharger ce fichier", "diese Datei herunterladen", "baixar este arquivo", "скачать этот файл", "このファイルをダウンロード", "이 파일 다운로드", "הורדת הקובץ הזה", "تنزيل هذا الملف"),
    # ── nothing to show ──
    "No audiobooks here yet.": ("这里还没有有声书。", "這裡還沒有有聲書。", "Aún no hay audiolibros aquí.", "Pas encore de livres audio ici.", "Hier sind noch keine Hörbücher.", "Ainda não há audiolivros aqui.", "Здесь пока нет аудиокниг.", "オーディオブックはまだありません。", "아직 오디오북이 없어요.", "עדיין אין כאן ספרי שמע.", "لا توجد كتب صوتية هنا بعد."),
    "A sound file stands here when it is an .m4b, when its name, title or album calls it an audiobook, or when it was fetched from the Audiobooks shelf of {usenet}.": (
        "音频文件在以下情况会出现在这里：它是 .m4b 文件，它的文件名、标题或专辑表明它是有声书，或者它是从 {usenet} 的有声书分类取回的。",
        "音訊檔在以下情況會出現在這裡：它是 .m4b 檔，它的檔名、標題或專輯表明它是有聲書，或者它是從 {usenet} 的有聲書分類取回的。",
        "Un archivo de sonido aparece aquí cuando es un .m4b, cuando su nombre, título o álbum lo llama audiolibro, o cuando se trajo de la sección Audiolibros de {usenet}.",
        "Un fichier audio apparaît ici quand c'est un .m4b, quand son nom, son titre ou son album le désigne comme livre audio, ou quand il a été récupéré dans le rayon Livres audio de {usenet}.",
        "Eine Audiodatei steht hier, wenn sie eine .m4b ist, wenn ihr Name, Titel oder Album sie als Hörbuch ausweist oder wenn sie aus dem Regal Hörbücher im {usenet} geholt wurde.",
        "Um arquivo de som aparece aqui quando é um .m4b, quando seu nome, título ou álbum o chama de audiolivro, ou quando foi trazido da seção Audiolivros da {usenet}.",
        "Аудиофайл попадает сюда, если это .m4b, если его имя, название или альбом говорят, что это аудиокнига, или если он получен с полки «Аудиокниги» в {usenet}.",
        "音声ファイルは、.m4b であるとき、ファイル名・タイトル・アルバムからオーディオブックとわかるとき、または {usenet} のオーディオブックの棚から取得したときに、ここに並びます。",
        "소리 파일은 .m4b이거나, 이름·제목·앨범에 오디오북이라고 되어 있거나, {usenet}의 오디오북 서가에서 가져온 경우 여기에 놓입니다.",
        "קובץ שמע מופיע כאן כשהוא .m4b, כששמו, כותרתו או האלבום שלו מציינים שהוא ספר שמע, או כשהובא ממדף ספרי השמע של {usenet}.",
        "يظهر الملف الصوتي هنا إذا كان بصيغة .m4b، أو إذا دلّ اسمه أو عنوانه أو ألبومه على أنه كتاب صوتي، أو إذا جُلب من رف الكتب الصوتية في {usenet}."),
    "No audio matches these filters.": ("没有符合这些筛选条件的音频。", "沒有符合這些篩選條件的音訊。", "Ningún audio coincide con estos filtros.", "Aucun audio ne correspond à ces filtres.", "Kein Audio passt zu diesen Filtern.", "Nenhum áudio corresponde a estes filtros.", "Нет аудио, подходящего под эти фильтры.", "この条件に合う音声はありません。", "이 조건에 맞는 오디오가 없습니다.", "אין שמע שמתאים לסינון הזה.", "لا يوجد صوت يطابق عوامل التصفية هذه."),
    "{n} audio indexed — click to fetch.": ("已索引 {n} 个音频，点击即可取回。", "已索引 {n} 個音訊，點一下即可取回。", "{n} audios indexados: haz clic para traerlos.", "{n} audios indexés : cliquez pour les récupérer.", "{n} Audiodateien erfasst – zum Holen anklicken.", "{n} áudios indexados: clique para trazer.", "Аудио в индексе: {n} — нажмите, чтобы получить.", "索引済みの音声 {n} 件 ― クリックで取得します。", "색인된 오디오 {n}개 - 누르면 가져옵니다.", "{n} קובצי שמע באינדקס – לחיצה מביאה אותם.", "الملفات الصوتية المفهرسة: {n} - انقر للجلب."),
    # ── the player at the bottom ──
    "— select a track —": ("— 选择一首曲目 —", "— 選擇一首曲目 —", "— elige una pista —", "— choisissez une piste —", "— Titel auswählen —", "— escolha uma faixa —", "— выберите трек —", "― 曲を選んでください ―", "— 곡을 선택하세요 —", "— בחרו רצועה —", "— اختر مقطعًا —"),
    "open the now-playing view": ("打开正在播放视图", "開啟正在播放畫面", "abrir la vista de reproducción", "ouvrir la vue de lecture en cours", "Wiedergabeansicht öffnen", "abrir a tela de reprodução", "открыть экран воспроизведения", "再生中の画面を開く", "지금 재생 중 화면 열기", "פתיחת תצוגת הניגון", "فتح عرض التشغيل الحالي"),
    "previous (p)": ("上一个 (p)", "上一個 (p)", "anterior (p)", "précédent (p)", "zurück (p)", "anterior (p)", "предыдущий (p)", "前へ (p)", "이전 (p)", "הקודם (p)", "السابق (p)"),
    "play / pause (space)": ("播放 / 暂停（空格键）", "播放 / 暫停（空白鍵）", "reproducir / pausar (espacio)", "lecture / pause (espace)", "Wiedergabe / Pause (Leertaste)", "tocar / pausar (espaço)", "играть / пауза (пробел)", "再生 / 一時停止（スペース）", "재생 / 일시정지 (스페이스)", "ניגון / השהיה (רווח)", "تشغيل / إيقاف مؤقت (مسافة)"),
    "next (n)": ("下一个 (n)", "下一個 (n)", "siguiente (n)", "suivant (n)", "weiter (n)", "próxima (n)", "следующий (n)", "次へ (n)", "다음 (n)", "הבא (n)", "التالي (n)"),
    "shuffle": ("随机播放", "隨機播放", "aleatorio", "lecture aléatoire", "Zufallswiedergabe", "aleatório", "вперемешку", "シャッフル", "셔플", "ערבוב", "تشغيل عشوائي"),
    "favourite (f)": ("收藏 (f)", "收藏 (f)", "favorito (f)", "favori (f)", "Favorit (f)", "favorito (f)", "в избранное (f)", "お気に入り (f)", "즐겨찾기 (f)", "מועדף (f)", "مفضلة (f)"),
    "cover, lyrics and spectrum (l)": ("封面、歌词和频谱 (l)", "封面、歌詞和頻譜 (l)", "portada, letra y espectro (l)", "pochette, paroles et spectre (l)", "Cover, Songtext und Spektrum (l)", "capa, letra e espectro (l)", "обложка, текст и спектр (l)", "ジャケット、歌詞、スペクトラム (l)", "표지, 가사, 스펙트럼 (l)", "עטיפה, מילים וספקטרום (l)", "الغلاف والكلمات والطيف (l)"),
    "Now playing": ("正在播放", "正在播放", "Reproduciendo", "Lecture en cours", "Läuft gerade", "Tocando agora", "Сейчас играет", "再生中", "지금 재생 중", "מתנגן עכשיו", "يُشغَّل الآن"),
    "could not play this file — try the next one": ("无法播放此文件，试试下一个", "無法播放此檔案，試試下一個", "no se pudo reproducir este archivo: prueba el siguiente", "impossible de lire ce fichier : essayez le suivant", "diese Datei lässt sich nicht abspielen – versuch die nächste", "não foi possível tocar este arquivo: tente o próximo", "не удалось воспроизвести этот файл — попробуйте следующий", "このファイルは再生できませんでした ― 次をお試しください", "이 파일을 재생할 수 없습니다 - 다음 것을 시도해 보세요", "לא ניתן לנגן את הקובץ הזה – נסו את הבא", "تعذّر تشغيل هذا الملف - جرّب التالي"),
    # ── the now-playing panel ──
    "Spectrum": ("频谱", "頻譜", "Espectro", "Spectre", "Spektrum", "Espectro", "Спектр", "スペクトラム", "스펙트럼", "ספקטרום", "الطيف"),
    "moving spectrum on / off": ("开 / 关动态频谱", "開 / 關動態頻譜", "espectro en movimiento sí / no", "spectre animé oui / non", "bewegtes Spektrum an / aus", "espectro em movimento liga / desliga", "движущийся спектр вкл. / выкл.", "動くスペクトラムのオン / オフ", "움직이는 스펙트럼 켜기 / 끄기", "ספקטרום נע: הפעלה / כיבוי", "الطيف المتحرك: تشغيل / إيقاف"),
    "Lyrics": ("歌词", "歌詞", "Letra", "Paroles", "Songtext", "Letra", "Текст", "歌詞", "가사", "מילים", "الكلمات"),
    "lyrics on / off": ("开 / 关歌词", "開 / 關歌詞", "letra sí / no", "paroles oui / non", "Songtext an / aus", "letra liga / desliga", "текст вкл. / выкл.", "歌詞のオン / オフ", "가사 켜기 / 끄기", "מילים: הפעלה / כיבוי", "الكلمات: تشغيل / إيقاف"),
    "Close": ("关闭", "關閉", "Cerrar", "Fermer", "Schließen", "Fechar", "Закрыть", "閉じる", "닫기", "סגירה", "إغلاق"),
    "looking for lyrics…": ("正在查找歌词…", "正在尋找歌詞…", "buscando la letra…", "recherche des paroles…", "Songtext wird gesucht…", "procurando a letra…", "ищем текст…", "歌詞を探しています…", "가사를 찾는 중…", "מחפש מילים…", "جارٍ البحث عن الكلمات…"),
    "No lyrics found for this track.": ("没有找到这首曲目的歌词。", "沒有找到這首曲目的歌詞。", "No se encontró la letra de esta pista.", "Aucune parole trouvée pour cette piste.", "Kein Songtext für diesen Titel gefunden.", "Nenhuma letra encontrada para esta faixa.", "Текст для этого трека не найден.", "この曲の歌詞は見つかりませんでした。", "이 곡의 가사를 찾지 못했습니다.", "לא נמצאו מילים לרצועה הזאת.", "لم يُعثر على كلمات لهذا المقطع."),
    "Could not load lyrics.": ("无法加载歌词。", "無法載入歌詞。", "No se pudo cargar la letra.", "Impossible de charger les paroles.", "Der Songtext konnte nicht geladen werden.", "Não foi possível carregar a letra.", "Не удалось загрузить текст.", "歌詞を読み込めませんでした。", "가사를 불러올 수 없습니다.", "לא ניתן לטעון את המילים.", "تعذّر تحميل الكلمات."),
}
