"""The words of the Releases page (/releases) and the Search page (/search): the chips, the
list view, the notes beside the heading, the tabs and the empty states.

English: (zh-Hans, zh-Hant, es, fr, de, pt, ru, ja, ko, he, ar) - see i18n.py.
"""

T = {
    # ── Releases ──
    "Episodes": ("剧集", "劇集", "Episodios", "Épisodes", "Folgen", "Episódios", "Серии", "エピソード", "에피소드", "פרקים", "الحلقات"),
    "Films and episodes": ("电影和剧集", "電影和劇集", "Películas y episodios", "Films et épisodes", "Filme und Folgen", "Filmes e episódios", "Фильмы и серии", "映画とエピソード", "영화와 에피소드", "סרטים ופרקים", "الأفلام والحلقات"),
    "Versions of this title": ("此片的所有版本", "此片的所有版本", "Versiones de este título", "Versions de ce titre", "Versionen dieses Titels", "Versões deste título", "Версии этого названия", "このタイトルのバージョン", "이 작품의 버전", "גרסאות של הכותר הזה", "نسخ هذا العنوان"),
    "best": ("最佳", "最佳", "mejores", "meilleurs", "beste", "melhores", "лучшие", "おすすめ順", "추천순", "הטובים", "الأفضل"),
    "known titles and good copies first": ("已识别的片名和质量好的版本排在前面", "已辨識的片名和品質好的版本排在前面", "primero los títulos reconocidos y las buenas copias", "titres reconnus et bonnes copies en premier", "bekannte Titel und gute Kopien zuerst", "primeiro os títulos reconhecidos e as boas cópias", "сначала известные названия и хорошие копии", "判明しているタイトルと良質なコピーを先に", "확인된 작품과 좋은 사본 먼저", "כותרים מזוהים ועותקים טובים תחילה", "العناوين المعروفة والنسخ الجيدة أولاً"),
    "most recently posted": ("最近发布的排在前面", "最近發布的排在前面", "lo publicado más recientemente", "publiés le plus récemment", "zuletzt gepostet", "publicados mais recentemente", "опубликованные недавно", "最近投稿されたもの", "가장 최근에 게시된 것", "מה שפורסם לאחרונה", "الأحدث نشراً"),
    "Grid": ("网格", "網格", "Cuadrícula", "Grille", "Raster", "Grade", "Сетка", "グリッド", "그리드", "רשת", "شبكة"),
    "List": ("列表", "清單", "Lista", "Liste", "Liste", "Lista", "Список", "リスト", "목록", "רשימה", "قائمة"),
    "No releases match this filter.": ("没有符合筛选条件的影视。", "沒有符合篩選條件的影視。", "Ningún estreno coincide con este filtro.", "Aucune sortie ne correspond à ce filtre.", "Keine Veröffentlichung passt zu diesem Filter.", "Nenhum lançamento corresponde a este filtro.", "Нет релизов, подходящих под этот фильтр.", "この条件に一致するリリースはありません。", "이 필터에 맞는 릴리스가 없습니다.", "אין מהדורות שתואמות לסינון הזה.", "لا توجد إصدارات تطابق هذا الفلتر."),
    "movie": ("电影", "電影", "película", "film", "Film", "filme", "фильм", "映画", "영화", "סרט", "فيلم"),
    "tv": ("剧集", "劇集", "serie", "série", "Serie", "série", "сериал", "ドラマ", "TV", "סדרה", "مسلسل"),
    "cat": ("类别", "類別", "tipo", "type", "Art", "tipo", "тип", "種類", "종류", "סוג", "النوع"),
    "quality": ("画质", "畫質", "calidad", "qualité", "Qualität", "qualidade", "качество", "画質", "화질", "איכות", "الجودة"),
    "{n} MB and larger": ("{n} MB 及以上", "{n} MB 以上", "{n} MB o más", "{n} Mo et plus", "ab {n} MB", "{n} MB ou mais", "от {n} МБ", "{n} MB 以上", "{n} MB 이상", "{n} MB ומעלה", "{n} ميغابايت فأكثر"),
    "show smaller too": ("也显示更小的", "也顯示更小的", "mostrar también los más pequeños", "afficher aussi les plus petits", "auch kleinere zeigen", "mostrar também os menores", "показать и меньшие", "小さいものも表示", "더 작은 것도 보기", "להציג גם קטנים יותר", "إظهار الأصغر أيضاً"),
    "all releases": ("全部影视", "全部影視", "todos los estrenos", "toutes les sorties", "alle Veröffentlichungen", "todos os lançamentos", "все релизы", "すべてのリリース", "전체 릴리스", "כל המהדורות", "كل الإصدارات"),
    "filter…": ("筛选…", "篩選…", "filtrar…", "filtrer…", "filtern…", "filtrar…", "фильтр…", "絞り込み…", "필터…", "סינון…", "تصفية…"),
    # ── Search ──
    "Details": ("详情", "詳細資訊", "Detalles", "Détails", "Details", "Detalhes", "Подробнее", "詳細", "자세히", "פרטים", "التفاصيل"),
    "Nothing found for %1": ("没有找到“%1”的结果", "找不到「%1」的結果", "No se encontró nada para %1", "Aucun résultat pour %1", "Nichts gefunden für %1", "Nada encontrado para %1", "По запросу %1 ничего не найдено", "%1 に一致するものはありません", "%1에 대한 결과가 없습니다", "לא נמצא דבר עבור %1", "لم يُعثر على شيء لـ %1"),
    "Try fewer words, another spelling, or one of the other tabs.": ("试试少用几个词、换一种拼写，或看看其他标签页。", "試試少用幾個詞、換一種拼法，或看看其他分頁。", "Prueba con menos palabras, otra ortografía o alguna de las otras pestañas.", "Essayez avec moins de mots, une autre orthographe ou l'un des autres onglets.", "Versuchen Sie es mit weniger Wörtern, einer anderen Schreibweise oder einem der anderen Tabs.", "Tente menos palavras, outra grafia ou uma das outras abas.", "Попробуйте меньше слов, другое написание или другую вкладку.", "単語を減らす、別の表記にする、または他のタブを試してください。", "단어를 줄이거나 철자를 바꾸거나 다른 탭을 확인해 보세요.", "נסו פחות מילים, איות אחר או אחת מהלשוניות האחרות.", "جرّب كلمات أقل، أو تهجئة أخرى، أو إحدى علامات التبويب الأخرى."),
    "Search everything tgarr has indexed.": ("搜索 tgarr 已索引的全部内容。", "搜尋 tgarr 已索引的全部內容。", "Busca en todo lo que tgarr ha indexado.", "Recherchez dans tout ce que tgarr a indexé.", "Durchsuchen Sie alles, was tgarr erfasst hat.", "Busque em tudo o que o tgarr indexou.", "Поиск по всему, что проиндексировал tgarr.", "tgarr がインデックスしたすべてを検索します。", "tgarr가 색인한 모든 것을 검색합니다.", "חיפוש בכל מה ש-tgarr אינדקס.", "ابحث في كل ما فهرسه tgarr."),
    "Films and episodes by title · any file by its name · channels by name or @username.": ("按片名找电影和剧集 · 按文件名找任意文件 · 按名称或 @用户名找频道。", "依片名找電影和劇集 · 依檔名找任意檔案 · 依名稱或 @使用者名稱找頻道。", "Películas y episodios por título · cualquier archivo por su nombre · canales por nombre o @usuario.", "Films et épisodes par titre · tout fichier par son nom · chaînes par nom ou @identifiant.", "Filme und Folgen nach Titel · jede Datei nach ihrem Namen · Kanäle nach Name oder @Benutzername.", "Filmes e episódios pelo título · qualquer arquivo pelo nome · canais pelo nome ou @usuário.", "Фильмы и серии по названию · любой файл по имени · каналы по названию или @имени.", "映画とエピソードはタイトルで · ファイルは名前で · チャンネルは名前か @ユーザー名で。", "영화와 에피소드는 제목으로 · 파일은 이름으로 · 채널은 이름이나 @사용자명으로.", "סרטים ופרקים לפי כותר · כל קובץ לפי שמו · ערוצים לפי שם או @שם משתמש.", "الأفلام والحلقات بالعنوان · أي ملف باسمه · القنوات بالاسم أو @اسم المستخدم."),
    "a film, a file name, a channel…": ("电影、文件名、频道…", "電影、檔名、頻道…", "una película, un nombre de archivo, un canal…", "un film, un nom de fichier, une chaîne…", "ein Film, ein Dateiname, ein Kanal…", "um filme, um nome de arquivo, um canal…", "фильм, имя файла, канал…", "映画、ファイル名、チャンネル…", "영화, 파일 이름, 채널…", "סרט, שם קובץ, ערוץ…", "فيلم، اسم ملف، قناة…"),
    "Films & episodes": ("电影和剧集", "電影和劇集", "Películas y episodios", "Films et épisodes", "Filme & Folgen", "Filmes e episódios", "Фильмы и серии", "映画とエピソード", "영화 · 에피소드", "סרטים ופרקים", "الأفلام والحلقات"),
    "All files": ("所有文件", "所有檔案", "Todos los archivos", "Tous les fichiers", "Alle Dateien", "Todos os arquivos", "Все файлы", "すべてのファイル", "모든 파일", "כל הקבצים", "كل الملفات"),
}
