"""The words on the pages, in the languages of the people who use them.

English is the text in the code; a translation is looked up by that English text. What has no
translation yet is shown in English - a page is never broken by a missing word.

    pick(cookie, accept_language, household)   which language for this request
    t(lang, "Find")                            the word in that language
    t(lang, "It opens again at {t}.", t="16:00")
    fill(lang, template)                       a page with [[@English text]] marks filled in;
                                               [[@js:English text]] for text inside a script

Which pages speak these languages so far: the Media Center, the PIN page, the page shown when
the Media Center is closed. (The rest of tgarr - the pages of whoever looks after it - is
English still; the catalogue below is where they will be added.)

The translations were written by the same hand as the code and have not been read by native
speakers of every language: corrections are welcome and cost one line each.
"""
import html
import json
import re

# (code, its own name). The order is the order of the tuples in T below.
LANGS = (("en", "English"), ("zh-Hans", "简体中文"), ("zh-Hant", "繁體中文"), ("es", "Español"), ("fr", "Français"), ("de", "Deutsch"),
         ("pt", "Português"), ("ru", "Русский"), ("ja", "日本語"), ("ko", "한국어"), ("he", "עברית"), ("ar", "العربية"))
CODES = tuple(c for c, _ in LANGS)
RTL = ("he", "ar")
COOKIE = "tgarr_lang"
_AT = {c: i - 1 for i, c in enumerate(CODES)}       # place in a tuple of T (English is the key, not in the tuple)

# English: (zh-Hans, zh-Hant, es, fr, de, pt, ru, ja, ko, he, ar)
T = {
    # ── the Media Center ──
    "Media Center": ("媒体中心", "媒體中心", "Centro multimedia", "Médiathèque", "Mediencenter", "Central de mídia", "Медиацентр", "メディアセンター", "미디어 센터", "מרכז המדיה", "مركز الوسائط"),
    "Watch": ("看", "看", "Ver", "Regarder", "Ansehen", "Assistir", "Смотреть", "見る", "보기", "לצפות", "مشاهدة"),
    "Listen": ("听", "聽", "Escuchar", "Écouter", "Anhören", "Ouvir", "Слушать", "聴く", "듣기", "להאזין", "استماع"),
    "Read": ("读", "讀", "Leer", "Lire", "Lesen", "Ler", "Читать", "読む", "읽기", "לקרוא", "قراءة"),
    "Photos": ("照片", "照片", "Fotos", "Photos", "Fotos", "Fotos", "Фото", "写真", "사진", "תמונות", "صور"),
    "What are you looking for?": ("想找什么？", "想找什麼？", "¿Qué buscas?", "Que cherchez-vous ?", "Was suchst du?", "O que você procura?", "Что ищем?", "何を探していますか？", "무엇을 찾고 있나요?", "מה מחפשים?", "عمّ تبحث؟"),
    "Find": ("查找", "搜尋", "Buscar", "Chercher", "Suchen", "Buscar", "Найти", "検索", "찾기", "חיפוש", "بحث"),
    "Carry on": ("继续看", "繼續看", "Continuar", "Reprendre", "Weitermachen", "Continuar", "Продолжить", "つづきから", "이어서 보기", "להמשיך", "متابعة"),
    "ready": ("已就绪", "已就緒", "listo", "prêt", "bereit", "pronto", "готово", "準備完了", "준비됨", "מוכן", "جاهز"),
    "Nothing found. Try another word?": ("没有找到。换个词试试？", "沒有找到。換個詞試試？", "No se encontró nada. ¿Probamos otra palabra?", "Rien trouvé. Essayer un autre mot ?", "Nichts gefunden. Ein anderes Wort versuchen?", "Nada encontrado. Tentar outra palavra?", "Ничего не найдено. Попробовать другое слово?", "見つかりません。別の言葉で試しますか？", "찾지 못했어요. 다른 낱말로 해 볼까요?", "לא נמצא דבר. לנסות מילה אחרת?", "لم يُعثر على شيء. هل نجرّب كلمة أخرى؟"),
    "Nothing here yet.": ("这里还没有内容。", "這裡還沒有內容。", "Aún no hay nada aquí.", "Rien ici pour l'instant.", "Hier ist noch nichts.", "Ainda não há nada aqui.", "Здесь пока ничего нет.", "まだ何もありません。", "아직 아무것도 없어요.", "עדיין אין כאן דבר.", "لا شيء هنا بعد."),
    "Things show up here once they are added to the Media Center.": ("内容加入媒体中心后就会出现在这里。", "內容加入媒體中心後就會出現在這裡。", "Las cosas aparecen aquí cuando se añaden al Centro multimedia.", "Les contenus apparaissent ici une fois ajoutés à la Médiathèque.", "Inhalte erscheinen hier, sobald sie dem Mediencenter hinzugefügt wurden.", "As coisas aparecem aqui quando são adicionadas à Central de mídia.", "Здесь появится то, что добавят в Медиацентр.", "メディアセンターに追加されると、ここに表示されます。", "미디어 센터에 추가되면 여기에 나타납니다.", "דברים יופיעו כאן אחרי שיתווספו למרכז המדיה.", "تظهر الأشياء هنا بعد إضافتها إلى مركز الوسائط."),
    "Back": ("上一页", "上一頁", "Atrás", "Retour", "Zurück", "Voltar", "Назад", "戻る", "뒤로", "חזרה", "رجوع"),
    "More": ("更多", "更多", "Más", "Suite", "Mehr", "Mais", "Ещё", "もっと見る", "더 보기", "עוד", "المزيد"),
    "Leave": ("离开", "離開", "Salir", "Quitter", "Verlassen", "Sair", "Выйти", "やめる", "나가기", "יציאה", "خروج"),
    "this is not me": ("这不是我", "這不是我", "no soy yo", "ce n'est pas moi", "das bin nicht ich", "não sou eu", "это не я", "自分ではありません", "제가 아니에요", "זה לא אני", "لستُ أنا"),
    "Enter PIN": ("输入 PIN", "輸入 PIN", "Introducir PIN", "Saisir le code", "PIN eingeben", "Digitar PIN", "Ввести PIN", "PIN を入力", "PIN 입력", "הזנת קוד", "إدخال الرمز"),
    "Lock": ("锁定", "鎖定", "Bloquear", "Verrouiller", "Sperren", "Bloquear", "Заблокировать", "ロック", "잠금", "נעילה", "قفل"),
    "Choose what is shown": ("选择显示的内容", "選擇顯示的內容", "Elegir qué se muestra", "Choisir ce qui est affiché", "Auswählen, was gezeigt wird", "Escolher o que é mostrado", "Выбрать, что показывать", "表示する内容を選ぶ", "보여줄 것 고르기", "בחירת התוכן המוצג", "اختيار ما يُعرض"),
    "Back to tgarr": ("返回 tgarr", "返回 tgarr", "Volver a tgarr", "Retour à tgarr", "Zurück zu tgarr", "Voltar ao tgarr", "Назад в tgarr", "tgarr に戻る", "tgarr로 돌아가기", "חזרה ל-tgarr", "العودة إلى tgarr"),
    # ── looks ──
    "Look": ("外观", "外觀", "Aspecto", "Apparence", "Aussehen", "Aparência", "Оформление", "見た目", "모양", "מראה", "المظهر"),
    "Language": ("语言", "語言", "Idioma", "Langue", "Sprache", "Idioma", "Язык", "言語", "언어", "שפה", "اللغة"),
    "Day & Night": ("日夜自动", "日夜自動", "Día y noche", "Jour et nuit", "Tag & Nacht", "Dia e noite", "День и ночь", "昼と夜", "낮과 밤", "יום ולילה", "نهار وليل"),
    "light by day, dark where the device is": ("跟随设备的浅色或深色", "跟隨裝置的淺色或深色", "claro u oscuro, como el dispositivo", "clair ou sombre, comme l'appareil", "hell oder dunkel, wie das Gerät", "claro ou escuro, como o aparelho", "светлое или тёмное, как на устройстве", "端末の設定に合わせて明るく／暗く", "기기 설정에 따라 밝게 또는 어둡게", "בהיר או כהה, לפי המכשיר", "فاتح أو داكن حسب الجهاز"),
    "Cinema": ("影院", "影院", "Cine", "Cinéma", "Kino", "Cinema", "Кинозал", "シネマ", "영화관", "קולנוע", "سينما"),
    "a dark room and the pictures - for a television": ("暗场加大海报，适合电视", "暗場加大海報，適合電視", "una sala oscura y las imágenes: para la tele", "une salle sombre et les images : pour la télé", "ein dunkler Raum und die Bilder – fürs Fernsehen", "uma sala escura e as imagens: para a TV", "тёмный зал и картинки — для телевизора", "暗い部屋と映像 ― テレビ向け", "어두운 방과 화면 - TV용", "חדר חשוך והתמונות – לטלוויזיה", "قاعة مظلمة والصور - للتلفاز"),
    "Playroom": ("游戏屋", "遊戲屋", "Sala de juegos", "Salle de jeux", "Spielzimmer", "Sala de brincar", "Игровая", "あそび部屋", "놀이방", "חדר משחקים", "غرفة اللعب"),
    "big, round and coloured - for the youngest": ("大、圆、彩色，适合小朋友", "大、圓、彩色，適合小朋友", "grande, redondo y de colores: para los más pequeños", "grand, rond et coloré : pour les plus petits", "groß, rund und bunt – für die Kleinsten", "grande, redondo e colorido: para os menores", "крупно, кругло и ярко — для самых маленьких", "大きく、まるく、カラフル ― 小さな子向け", "크고 둥글고 알록달록 - 어린이용", "גדול, עגול וצבעוני – לקטנים", "كبير ومستدير وملوّن - للصغار"),
    "Paper": ("纸页", "紙頁", "Papel", "Papier", "Papier", "Papel", "Бумага", "紙", "종이", "נייר", "ورق"),
    "quiet, like a shelf of books": ("安静，像一排书架", "安靜，像一排書架", "tranquilo, como una estantería", "calme, comme une étagère de livres", "ruhig, wie ein Bücherregal", "tranquilo, como uma estante de livros", "спокойно, как книжная полка", "静かな本棚のように", "책장처럼 차분하게", "שקט, כמו מדף ספרים", "هادئ، كرفّ من الكتب"),
    "Aurora": ("极光", "極光", "Aurora", "Aurore", "Polarlicht", "Aurora", "Северное сияние", "オーロラ", "오로라", "זוהר", "شفق"),
    "glass over a night sky": ("夜空上的玻璃质感", "夜空上的玻璃質感", "cristal sobre un cielo nocturno", "du verre sur un ciel de nuit", "Glas über einem Nachthimmel", "vidro sobre um céu noturno", "стекло над ночным небом", "夜空に重ねたガラス", "밤하늘 위의 유리", "זכוכית מעל שמי לילה", "زجاج فوق سماء الليل"),
    "Clear": ("清晰", "清晰", "Nítido", "Net", "Klar", "Nítido", "Чётко", "くっきり", "또렷하게", "ברור", "واضح"),
    "black, white and yellow, large": ("黑白黄高对比，大字", "黑白黃高對比，大字", "negro, blanco y amarillo, en grande", "noir, blanc et jaune, en grand", "Schwarz, Weiß und Gelb, groß", "preto, branco e amarelo, grande", "чёрное, белое и жёлтое, крупно", "黒・白・黄色、大きな文字", "검정, 하양, 노랑, 크게", "שחור, לבן וצהוב, גדול", "أسود وأبيض وأصفر، بحجم كبير"),
    "Always light": ("始终浅色", "始終淺色", "Siempre claro", "Toujours clair", "Immer hell", "Sempre claro", "Всегда светлое", "常に明るく", "항상 밝게", "תמיד בהיר", "فاتح دائمًا"),
    "Always dark": ("始终深色", "始終深色", "Siempre oscuro", "Toujours sombre", "Immer dunkel", "Sempre escuro", "Всегда тёмное", "常に暗く", "항상 어둡게", "תמיד כהה", "داكن دائمًا"),
    "Make this the look every screen starts with": ("设为所有屏幕的默认外观", "設為所有螢幕的預設外觀", "Usar este aspecto al empezar en todas las pantallas", "En faire l'apparence de départ de tous les écrans", "Als Startaussehen für alle Bildschirme festlegen", "Usar esta aparência ao iniciar em todas as telas", "Сделать оформлением по умолчанию для всех экранов", "すべての画面の最初の見た目にする", "모든 화면의 기본 모양으로 하기", "להפוך למראה ההתחלתי בכל המסכים", "اجعله المظهر الافتراضي لكل الشاشات"),
    "every screen now starts with this look": ("所有屏幕现在默认用这个外观", "所有螢幕現在預設用這個外觀", "todas las pantallas empiezan ahora con este aspecto", "tous les écrans démarrent désormais avec cette apparence", "alle Bildschirme starten jetzt mit diesem Aussehen", "todas as telas agora começam com esta aparência", "теперь все экраны начинают с этого оформления", "すべての画面がこの見た目で始まります", "이제 모든 화면이 이 모양으로 시작합니다", "כל המסכים מתחילים עכשיו במראה הזה", "كل الشاشات تبدأ الآن بهذا المظهر"),
    "Make this the language every screen starts with": ("设为所有屏幕的默认语言", "設為所有螢幕的預設語言", "Usar este idioma al empezar en todas las pantallas", "En faire la langue de départ de tous les écrans", "Als Startsprache für alle Bildschirme festlegen", "Usar este idioma ao iniciar em todas as telas", "Сделать языком по умолчанию для всех экранов", "すべての画面の最初の言語にする", "모든 화면의 기본 언어로 하기", "להפוך לשפת ההתחלה בכל המסכים", "اجعلها اللغة الافتراضية لكل الشاشات"),
    "every screen now starts in this language": ("所有屏幕现在默认用这种语言", "所有螢幕現在預設用這種語言", "todas las pantallas empiezan ahora en este idioma", "tous les écrans démarrent désormais dans cette langue", "alle Bildschirme starten jetzt in dieser Sprache", "todas as telas agora começam neste idioma", "теперь все экраны начинают на этом языке", "すべての画面がこの言語で始まります", "이제 모든 화면이 이 언어로 시작합니다", "כל המסכים מתחילים עכשיו בשפה הזאת", "كل الشاشات تبدأ الآن بهذه اللغة"),
    "not saved": ("未保存", "未儲存", "no se guardó", "non enregistré", "nicht gespeichert", "não salvo", "не сохранено", "保存できませんでした", "저장되지 않았습니다", "לא נשמר", "لم يُحفظ"),
    # ── the PIN page ──
    "Enter the PIN": ("输入 PIN", "輸入 PIN", "Introduce el PIN", "Saisissez le code", "PIN eingeben", "Digite o PIN", "Введите PIN", "PIN を入力してください", "PIN을 입력하세요", "הזינו את הקוד", "أدخل الرمز"),
    "Enter the PIN to open all of tgarr.": ("输入 PIN 以打开完整的 tgarr。", "輸入 PIN 以開啟完整的 tgarr。", "Introduce el PIN para abrir todo tgarr.", "Saisissez le code pour ouvrir tout tgarr.", "Gib die PIN ein, um ganz tgarr zu öffnen.", "Digite o PIN para abrir todo o tgarr.", "Введите PIN, чтобы открыть весь tgarr.", "PIN を入力すると tgarr のすべてが開きます。", "PIN을 입력하면 tgarr 전체가 열립니다.", "הזינו את הקוד כדי לפתוח את כל tgarr.", "أدخل الرمز لفتح tgarr كاملًا."),
    "keep this browser unlocked for 30 days": ("此浏览器 30 天内保持解锁", "此瀏覽器 30 天內保持解鎖", "mantener este navegador desbloqueado 30 días", "garder ce navigateur déverrouillé 30 jours", "diesen Browser 30 Tage entsperrt lassen", "manter este navegador desbloqueado por 30 dias", "не блокировать этот браузер 30 дней", "このブラウザーを 30 日間ロック解除のままにする", "이 브라우저를 30일 동안 잠금 해제 상태로 두기", "להשאיר את הדפדפן הזה פתוח ל-30 יום", "إبقاء هذا المتصفح مفتوحًا 30 يومًا"),
    "Unlock": ("解锁", "解鎖", "Desbloquear", "Déverrouiller", "Entsperren", "Desbloquear", "Разблокировать", "ロック解除", "잠금 해제", "פתיחה", "فتح القفل"),
    "back to the Media Center": ("返回媒体中心", "返回媒體中心", "volver al Centro multimedia", "retour à la Médiathèque", "zurück zum Mediencenter", "voltar à Central de mídia", "назад в Медиацентр", "メディアセンターに戻る", "미디어 센터로 돌아가기", "חזרה למרכז המדיה", "العودة إلى مركز الوسائط"),
    "That is not the PIN.": ("PIN 不对。", "PIN 不對。", "Ese no es el PIN.", "Ce n'est pas le bon code.", "Das ist nicht die PIN.", "Esse não é o PIN.", "Это не тот PIN.", "PIN が違います。", "PIN이 맞지 않습니다.", "זה לא הקוד.", "هذا ليس الرمز."),
    "Could not reach tgarr.": ("无法连接 tgarr。", "無法連線 tgarr。", "No se pudo conectar con tgarr.", "Impossible de joindre tgarr.", "tgarr ist nicht erreichbar.", "Não foi possível conectar ao tgarr.", "Не удалось связаться с tgarr.", "tgarr に接続できませんでした。", "tgarr에 연결할 수 없습니다.", "לא ניתן להגיע אל tgarr.", "تعذّر الوصول إلى tgarr."),
    # ── closed for now ──
    "The Media Center is closed now": ("媒体中心现在休息了", "媒體中心現在休息了", "El Centro multimedia está cerrado ahora", "La Médiathèque est fermée pour l'instant", "Das Mediencenter ist jetzt geschlossen", "A Central de mídia está fechada agora", "Медиацентр сейчас закрыт", "メディアセンターはいま閉まっています", "미디어 센터는 지금 닫혀 있어요", "מרכז המדיה סגור עכשיו", "مركز الوسائط مغلق الآن"),
    "It opens again at {t}.": ("{t} 再开放。", "{t} 再開放。", "Vuelve a abrir a las {t}.", "Elle rouvre à {t}.", "Es öffnet wieder um {t}.", "Abre de novo às {t}.", "Снова откроется в {t}.", "{t} にまた開きます。", "{t}에 다시 열려요.", "ייפתח שוב ב-{t}.", "يفتح من جديد الساعة {t}."),
    "That is all for today": ("今天就到这里", "今天就到這裡", "Eso es todo por hoy", "C'est tout pour aujourd'hui", "Das war's für heute", "Por hoje é só", "На сегодня всё", "今日はここまで", "오늘은 여기까지", "זה הכול להיום", "هذا كل شيء لليوم"),
    "Well done. See you tomorrow!": ("做得很好，明天见！", "做得很好，明天見！", "Muy bien. ¡Hasta mañana!", "Bravo. À demain !", "Gut gemacht. Bis morgen!", "Muito bem. Até amanhã!", "Молодец. До завтра!", "よくできました。また明日！", "잘했어요. 내일 만나요!", "כל הכבוד. נתראה מחר!", "أحسنت. نراك غدًا!"),
    "Time for a break": ("该休息一下了", "該休息一下了", "Hora de un descanso", "C'est l'heure de la pause", "Zeit für eine Pause", "Hora de uma pausa", "Пора отдохнуть", "休憩の時間です", "쉬는 시간이에요", "זמן להפסקה", "حان وقت الاستراحة"),
    "Stretch, drink some water, look far away. Back at {t}.": ("伸伸懒腰，喝点水，看看远处。{t} 再回来。", "伸伸懶腰，喝點水，看看遠處。{t} 再回來。", "Estírate, bebe agua, mira a lo lejos. Volvemos a las {t}.", "Étire-toi, bois un peu d'eau, regarde au loin. Retour à {t}.", "Streck dich, trink etwas Wasser, schau in die Ferne. Weiter um {t}.", "Alongue-se, beba água, olhe para longe. Voltamos às {t}.", "Потянись, выпей воды, посмотри вдаль. Продолжим в {t}.", "体を伸ばして、水を飲んで、遠くを見ましょう。{t} に再開します。", "기지개를 켜고, 물을 마시고, 먼 곳을 보세요. {t}에 다시 만나요.", "להתמתח, לשתות מים, להביט רחוק. חוזרים ב-{t}.", "تمدّد، اشرب بعض الماء، وانظر بعيدًا. نعود الساعة {t}."),
}


def norm(code: str) -> str:
    """'zh-CN' -> 'zh-Hans', 'pt-BR' -> 'pt', 'EN_us' -> 'en'; '' for a language not spoken here."""
    c = (code or "").strip().replace("_", "-").lower()
    if not c:
        return ""
    if c.startswith("zh"):
        return "zh-Hant" if any(x in c for x in ("hant", "-tw", "-hk", "-mo")) else "zh-Hans"
    if c.startswith("iw"):                       # the old code for Hebrew
        return "he"
    base = c.split("-")[0]
    return base if base in CODES else ""


def pick(cookie: str = "", accept: str = "", household: str = "") -> str:
    """The language of a request: what this screen chose, else what the household set, else
    the first language the browser asks for that is spoken here, else English."""
    for own in (cookie, household):
        n = norm(own)
        if n:
            return n
    asked = []
    for part in (accept or "").split(",")[:20]:
        tag, _, q = part.strip().partition(";q=")
        try:
            weight = float(q) if q else 1.0
        except ValueError:
            weight = 0.0
        asked.append((-weight, len(asked), tag))
    for _, _, tag in sorted(asked):
        n = norm(tag)
        if n:
            return n
    return "en"


def t(lang: str, text: str, **values) -> str:
    row = T.get(text)
    at = _AT.get(lang, -1)
    out = row[at] if row and 0 <= at < len(row) and row[at] else text
    if values:
        try:
            return out.format(**values)
        except (KeyError, IndexError, ValueError):
            return text.format(**values)
    return out


_MARK = re.compile(r"\[\[@(js:)?(.+?)\]\]")


def fill(lang: str, page: str) -> str:
    """A page whose words are written as [[@English]] (the @ keeps a list inside a list of a script from being taken for one): each replaced by the word in `lang`,
    made safe for where it stands (HTML, or inside a quoted string of a script)."""
    def one(m):
        word = t(lang, m.group(2))
        return json.dumps(word, ensure_ascii=False)[1:-1].replace("</", "<\\/") if m.group(1) else html.escape(word, quote=True)
    return _MARK.sub(one, page)


def direction(lang: str) -> str:
    return "rtl" if lang in RTL else "ltr"
