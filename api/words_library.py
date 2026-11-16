"""The words of the Library page (/library): the counters, the chips, the book cards.

English: (zh-Hans, zh-Hant, es, fr, de, pt, ru, ja, ko, he, ar) - see i18n.py.
"""

T = {
    "{n} cached": ("已下载 {n} 本", "已下載 {n} 本", "{n} descargados", "{n} téléchargés", "{n} heruntergeladen", "{n} baixados", "скачано: {n}", "ダウンロード済み {n} 冊", "내려받은 책 {n}권", "{n} הורדו", "تم تنزيل {n}"),
    "No ebooks cached yet.": ("还没有下载电子书。", "還沒有下載電子書。", "Aún no hay libros descargados.", "Aucun livre téléchargé pour l'instant.", "Noch keine Bücher heruntergeladen.", "Ainda não há livros baixados.", "Пока нет скачанных книг.", "ダウンロード済みの本はまだありません。", "아직 내려받은 책이 없습니다.", "עדיין לא הורדו ספרים.", "لم تُنزَّل أي كتب بعد."),
    "{n} ebooks indexed — click to fetch.": ("已索引 {n} 本电子书，点击即可取回。", "已索引 {n} 本電子書，點擊即可取回。", "{n} libros indexados: haz clic para traerlos.", "{n} livres indexés : cliquez pour les récupérer.", "{n} Bücher erfasst – zum Holen anklicken.", "{n} livros indexados: clique para trazê-los.", "Книг в индексе: {n} — нажмите, чтобы получить.", "{n} 冊を索引済み ― クリックで取得します。", "책 {n}권 색인됨 - 누르면 가져옵니다.", "{n} ספרים באינדקס – לחצו כדי להביא.", "الكتب المفهرسة: {n} - انقر لجلبها."),
    "{n} indexed (click to fetch)": ("已索引 {n} 本（点击即可取回）", "已索引 {n} 本（點擊即可取回）", "{n} indexados (clic para traer)", "{n} indexés (cliquer pour récupérer)", "{n} erfasst (zum Holen anklicken)", "{n} indexados (clique para trazer)", "в индексе: {n} (нажмите, чтобы получить)", "索引済み {n} 冊（クリックで取得）", "색인됨 {n}권 (누르면 가져옴)", "{n} באינדקס (לחצו כדי להביא)", "المفهرسة: {n} (انقر للجلب)"),
    "{n} of {total}": ("{n} / {total}", "{n} / {total}", "{n} de {total}", "{n} sur {total}", "{n} von {total}", "{n} de {total}", "{n} из {total}", "{total} 件中 {n} 件", "{total}개 중 {n}개", "{n} מתוך {total}", "{n} من {total}"),
    "No ebooks match these filters.": ("没有符合这些筛选条件的电子书。", "沒有符合這些篩選條件的電子書。", "Ningún libro coincide con estos filtros.", "Aucun livre ne correspond à ces filtres.", "Keine Bücher passen zu diesen Filtern.", "Nenhum livro corresponde a estes filtros.", "Нет книг, подходящих под эти фильтры.", "この条件に合う本はありません。", "이 조건에 맞는 책이 없습니다.", "אין ספרים שמתאימים לסינון הזה.", "لا توجد كتب تطابق عوامل التصفية هذه."),
    "all": ("全部", "全部", "todos", "tous", "alle", "todos", "все", "すべて", "전체", "הכול", "الكل"),
    "Sort:": ("排序：", "排序：", "Orden:", "Tri :", "Sortierung:", "Ordem:", "Сортировка:", "並べ替え:", "정렬:", "מיון:", "الترتيب:"),
    "Best": ("最佳", "最佳", "Mejores", "Meilleurs", "Beste", "Melhores", "Лучшие", "ベスト", "베스트", "הטובים ביותר", "الأفضل"),
    "Recent": ("最新", "最新", "Recientes", "Récents", "Neueste", "Recentes", "Новые", "新着", "최신", "אחרונים", "الأحدث"),
    "Largest": ("最大", "最大", "Más grandes", "Plus gros", "Größte", "Maiores", "Большие", "大きい順", "큰 파일순", "הגדולים ביותר", "الأكبر"),
    "Title": ("书名", "書名", "Título", "Titre", "Titel", "Título", "Название", "タイトル", "제목", "כותרת", "العنوان"),
    "Audiobooks": ("有声书", "有聲書", "Audiolibros", "Livres audio", "Hörbücher", "Audiolivros", "Аудиокниги", "オーディオブック", "오디오북", "ספרי שמע", "الكتب الصوتية"),
    "books that are listened to": ("用来听的书", "用來聽的書", "libros para escuchar", "des livres à écouter", "Bücher zum Anhören", "livros para ouvir", "книги, которые слушают", "聴く本", "듣는 책", "ספרים להאזנה", "كتب تُسمَع"),
    "(untitled)": ("（无标题）", "（無標題）", "(sin título)", "(sans titre)", "(ohne Titel)", "(sem título)", "(без названия)", "（無題）", "(제목 없음)", "(ללא שם)", "(بلا عنوان)"),
    "Open": ("打开", "開啟", "Abrir", "Ouvrir", "Öffnen", "Abrir", "Открыть", "開く", "열기", "פתיחה", "فتح"),
    "Download": ("下载", "下載", "Descargar", "Télécharger", "Herunterladen", "Baixar", "Скачать", "ダウンロード", "다운로드", "הורדה", "تنزيل"),
}
