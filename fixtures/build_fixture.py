# -*- coding: utf-8 -*-
r"""生成端到端测试用的**合成语料夹具**。

## 为什么需要它

`e2e/` 下那六个脚本真跑 `getterms.run`（只把 LLM 客户端换成假的，其余全是真的）。
它们原来读的是生产语料 —— 而**生产语料不能进公开仓库**。
所以这里给一份**完全自造**的微型语料：形状与真语料**逐字段相同**，
内容是虚构的（一座不存在的城市、一座不存在的博物馆）。

**这份夹具里的每一句都是本项目自己写的，不来自任何真实语料。**

## 形状

与 `align.qc.json` 一致：顶层 `align_id / scene / domain / pair / session_no /
seg_no / src_audio / tgt_audio / sentences / info`，
每句 `sent_id / src{start_ms,end_ms,text} / tgt{...} / notes`。

文件名按约定：`{lang-lang}_{scene}_{domain}_{session}_{seg}_align.qc.json`。

## 用法

    python fixtures/build_fixture.py     # 重新生成 corpus_es/

生成的 `corpus_es/` 直接给 `e2e/` 下的脚本用（脚本会把它挂到 `config.CORPUS_DIRS`）。
"""
from __future__ import annotations

import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "corpus_es"
NAME = "zh-es_conf_poli_0001_seg001_align.qc.json"

# 虚构的一期节目：滨海市博物馆的开馆导览。中文在前、西语在后，逐句对应。
PAIRS: list[tuple[str, str]] = [
    ("欢迎各位来到滨海市博物馆。", "Bienvenidos al Museo de la Ciudad de Binhai."),
    ("本馆收藏着这座城市两千年的记忆。", "Este museo conserva la memoria de dos mil años de la ciudad."),
    ("展厅分为四个部分，沿时间顺序排列。",
     "Las salas de exposición se dividen en cuatro secciones, ordenadas cronológicamente."),
    ("第一件展品是一艘出土的独木舟。", "La primera pieza es una canoa excavada."),
    ("它来自公元前三世纪的渔村遗址。", "Procede de un yacimiento pesquero del siglo tercero antes de nuestra era."),
    ("考古队在河道清淤时发现了它。",
     "El equipo arqueológico la descubrió durante el dragado del canal."),
    ("船体由整块木材凿成，没有拼接。",
     "El casco está tallado en una sola pieza de madera, sin ensamblajes."),
    ("接下来的展柜里是历代的盐业文书。",
     "En las vitrinas siguientes se conservan documentos salineros de distintas dinastías."),
    ("盐税曾是这座城市最主要的收入。",
     "El impuesto sobre la sal fue la principal fuente de ingresos de la ciudad."),
    ("文书上的印章保存得相当完整。", "Los sellos de los documentos se conservan bastante completos."),
    ("这里展示的是宋代的市舶司遗址模型。",
     "Aquí se exhibe una maqueta del yacimiento de la oficina de navegación de la dinastía Song."),
    ("当时的商船从这里出发，前往东南亚。",
     "Los barcos mercantes partían de aquí hacia el sudeste asiático."),
    ("模型按一比五十的比例制作。", "La maqueta está realizada a escala uno a cincuenta."),
    ("展柜旁边有一块出土的船板。", "Junto a la vitrina hay una tablazón naval excavada."),
    ("船板上的榫卯结构清晰可见。", "Las uniones de caja y espiga son claramente visibles en la tablazón."),
    ("第二部分讲的是明清时期的海防。", "La segunda sección trata de la defensa costera durante las dinastías Ming y Qing."),
    ("沿海修筑了多座烽火台。", "Se construyeron varias torres de señales a lo largo de la costa."),
    ("这些炮台的射程覆盖整个港湾。",
     "El alcance de estas baterías cubría toda la bahía."),
    ("展厅中央陈列着一门铁炮。", "En el centro de la sala se exhibe un cañón de hierro."),
    ("炮身上的铭文记录了铸造年份。",
     "La inscripción del cañón registra el año de fundición."),
    ("它在一九八七年由渔民打捞出水。",
     "Fue recuperado del agua por unos pescadores en mil novecientos ochenta y siete."),
    ("第三部分是近代的港口建设。", "La tercera sección aborda la construcción portuaria moderna."),
    ("一八六一年，这里开埠通商。", "En mil ochocientos sesenta y uno se abrió este puerto al comercio."),
    ("外国商行在岸边建起了仓库。", "Las casas de comercio extranjeras levantaron almacenes en la ribera."),
    ("这些建筑如今还剩七栋。", "Todavía quedan siete de aquellos edificios."),
    ("其中一栋改成了今天的博物馆。",
     "Uno de ellos se transformó en el museo actual."),
    ("墙面上保留着当年的砖砌纹理。",
     "Los muros conservan la textura original de los ladrillos."),
    ("天花板下的钢梁是后来加固的。",
     "Las vigas de acero bajo el techo se añadieron después para reforzar la estructura."),
    ("第四部分展示当代的城市规划。",
     "La cuarta sección muestra la planificación urbana contemporánea."),
    ("沙盘上可以看到未来十年的建设蓝图。",
     "En la maqueta puede verse el plan de construcción para los próximos diez años."),
    ("新区将建设三条地铁线路。",
     "En el nuevo distrito se construirán tres líneas de metro."),
    ("老城区的街道保持原有宽度。",
     "Las calles del casco antiguo mantienen su anchura original."),
    ("沿岸的湿地被划为保护区。", "Los humedales costeros fueron declarados zona protegida."),
    ("每年冬天有大批候鸟在此停留。",
     "Cada invierno se detienen aquí grandes bandadas de aves migratorias."),
    ("保护区里设置了观鸟屋。", "En la reserva se instalaron observatorios de aves."),
    ("参观者需要提前预约。", "Los visitantes deben reservar con antelación."),
    ("博物馆每周一闭馆。", "El museo cierra los lunes."),
    ("常规展览免费向公众开放。",
     "Las exposiciones permanentes son gratuitas para el público."),
    ("特展需要单独购票。", "Las exposiciones temporales requieren una entrada aparte."),
    ("语音导览提供四种语言。",
     "La audioguía está disponible en cuatro idiomas."),
    ("团队参观可以预约讲解员。",
     "Los grupos pueden reservar un guía."),
    ("讲解时长约为九十分钟。", "La visita guiada dura aproximadamente noventa minutos."),
    ("馆内的文创商店在出口处。",
     "La tienda del museo se encuentra junto a la salida."),
    ("商店里出售明信片和复制品。",
     "En la tienda se venden postales y reproducciones."),
    ("最受欢迎的是独木舟的模型。",
     "El más popular es el modelo de la canoa."),
    ("咖啡厅提供简餐和饮料。", "La cafetería ofrece comidas ligeras y bebidas."),
    ("露台可以看到整个港湾。",
     "Desde la terraza se contempla toda la bahía."),
    ("傍晚的景色尤其好看。", "El paisaje al atardecer es especialmente bello."),
    ("博物馆还开设面向学生的课程。",
     "El museo también imparte cursos dirigidos a estudiantes."),
    ("课程内容包括考古绘图和文物修复。",
     "Los cursos incluyen dibujo arqueológico y restauración de objetos."),
    ("每年暑期招募志愿者。",
     "Cada verano se convoca a voluntarios."),
    ("志愿者需要接受两周培训。",
     "Los voluntarios reciben dos semanas de formación."),
    ("培训由馆内的研究人员负责。",
     "La formación corre a cargo de los investigadores del museo."),
    ("研究工作主要集中在陶瓷和金属器。",
     "La investigación se centra sobre todo en cerámicas y objetos metálicos."),
    ("实验室配备了便携式检测设备。",
     "El laboratorio cuenta con equipos portátiles de análisis."),
    ("部分检测可以在展柜旁完成。",
     "Algunos análisis pueden realizarse junto a las vitrinas."),
    ("这样能减少文物搬运的风险。",
     "Así se reduce el riesgo de trasladar las piezas."),
    ("所有修复记录都会公开。",
     "Todos los registros de restauración se hacen públicos."),
    ("数据库可以在网站上查询。",
     "La base de datos puede consultarse en el sitio web."),
    ("感谢各位的参观。", "Gracias por su visita."),
    ("出口在展厅的左手边。", "La salida está a la izquierda de la sala."),
]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    sentences = []
    for i, (zh, es) in enumerate(PAIRS, start=1):
        s, e = i * 4, i * 4 + 3
        sentences.append({
            "sent_id": i,
            "src": {"start_ms": f"00:00:{s:02d},000", "end_ms": f"00:00:{e:02d},900",
                    "text": zh},
            "tgt": {"start_ms": f"00:00:{s:02d},000", "end_ms": f"00:00:{e:02d},900",
                    "text": es},
            "notes": "",
        })
    doc = {
        "align_id": "demo-0001",
        "scene": "conf",
        "domain": "poli",
        "pair": "zh-es",
        "session_no": "0001",
        "seg_no": "seg001",
        "src_audio": "sample/zh_conf_poli_0001_seg001_src.wav",
        "tgt_audio": "sample/zh_conf_poli_0001_seg001_tgt.wav",
        "sentences": sentences,
        "info": {"align_time": "2026-01-01 00:00:00", "note": "合成夹具，内容全部虚构"},
    }
    p = OUT / NAME
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"写出 {p}（{len(sentences)} 句）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
