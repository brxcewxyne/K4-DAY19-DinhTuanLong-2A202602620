# Báo cáo Day 19 — Flat RAG vs GraphRAG

**Họ tên:** Đinh Tuấn Long  **MSSV:** 2A202602620  **Ngày:** 2026-10-05

> Kỳ vọng và thang điểm: `SUBMISSION.md`. Mọi số liệu dưới đây copy từ `ket_qua_benchmark_kg.txt` (chat `openai:gpt-4o-mini`, embedding `openai:text-embedding-3-small`, top_k=3, chunk_size=800, 176 chunk, KG 607 node / 1215 cạnh). Bản thiết kế ontology ở `report/ONTOLOGY.md`.

## 1. Chi phí (10 điểm)

Hai bảng từ `ket_qua_benchmark_kg.txt`:

```
== Indexing (one-off)
pipeline  calls    in_tok  out_tok       USD  seconds
flat        176     56072        0   0.00112     75.5
graph       196     92878     5142   0.00973    144.8

== Querying (mean per question)
pipeline  recall  judge   in_tok  out_tok       USD  seconds
flat        0.43   1.00      694       47   0.00013     1.29
graph       0.83   1.67     3964       90   0.00064     1.97
```

| Chỉ số | Flat | Graph | Graph / Flat |
| --- | --- | --- | --- |
| Indexing USD | 0.00112 | 0.00973 | ×8.69 |
| Indexing giây | 75.5 | 144.8 | ×1.92 |
| Mỗi câu: USD | 0.00013 | 0.00064 | ×4.92 |
| Mỗi câu: giây | 1.29 | 1.97 | ×1.53 |
| Mỗi câu: in_tok | 694 | 3964 | ×5.71 |

**Chi phí tăng thêm đến từ đâu?** Indexing Graph = embedding 176 chunk (như Flat) + **20 lượt gọi LLM trích xuất tin** (92.878 in / 5.142 out token → +$0,00861, +69,3 giây). Mỗi câu GraphRAG gửi thêm khoảng 3.270 input token dữ kiện đồ thị (~5,7×) và tốn thêm ~0,68 giây. **Điểm hòa vốn ước tính:** $0,00861 / $0,00051 ≈ **17 câu hỏi** (mới tính token, chưa tính độ trễ).

## 2. Từng câu hỏi (10 điểm)

| Câu | Loại | Flat recall / judge | Graph recall / judge | Thắng | Vì sao (1 câu) |
| --- | --- | --- | --- | --- | --- |
| Q1 | single-hop-law | 1.00 / 2 | 1.00 / 2 | Hòa (Flat rẻ hơn) | Định nghĩa "tiền chất" nằm gọn trong 1 chunk của `pcmt-dieu-2`, graph không thêm giá trị |
| Q2 | single-hop-news | 1.00 / 2 | 1.00 / 2 | Hòa (Flat rẻ hơn) | Hai bị cáo và án tử hình nằm trong cùng một bài 36kg |
| Q3 | cross-kb | 0.00 / 0 | 1.00 / 2 | **Graph** | Flat trả "Không đủ thông tin" vì tên người ở KB tin, Điều 251 + khung 02–07 năm ở KB luật |
| Q4 | cross-kb | 0.00 / 0 | 1.00 / 1 | **Graph (lõi)** | Graph nối đúng Hoàng Nato → Điều 255 khoản 4 (20 năm/chung thân), nhưng thêm charge "mua bán" do over-extraction |
| Q5 | cross-kb-multi-hop | 0.60 / 1 | 1.00 / 2 | **Graph** | Chỉ graph so được 9,6kg MDMA với ngưỡng ≥100g để chốt **Điều 250 khoản 4** |
| Q6 | aggregation | 0.00 / 1 | 0.00 / 1 | Hòa điểm, graph đủ dữ kiện hơn | Cả hai nêu đúng 3 vụ MDMA nhưng không chứa đúng chuỗi `must_include` |

Quy luật rút ra: **GraphRAG thắng khi câu hỏi buộc ghép ≥2 tài liệu hoặc ghép người ↔ vụ ↔ tội ↔ điều/khoản/ngưỡng (Q3, Q5, lõi Q4); Flat RAG đủ và rẻ hơn ~5× cho câu single-document (Q1, Q2).**

## 3. Phân tích lỗi (20 điểm)

### Lỗi E4: Phép đo recall lệch judge — Q6 aggregation

- **Hiện tượng:** cả hai pipeline đều bị `recall=0.00` nhưng `judge=1`; câu trả lời liệt kê đúng 3 vụ MDMA (Cái Quang Huy, Lê Minh Thành, vụ Sầm Sơn/Viện Pháp y) nhưng dùng tên vụ thay vì tên người/tổ chức trong `must_include`.
- **Bằng chứng:** câu trả lời GraphRAG (dòng 43–49 `ket_qua_benchmark_kg.txt`):

```text
1. **Vụ vận chuyển ma túy từ Đức về Việt Nam**: Hơn 9,6kg MDMA đã được vận chuyển...
2. **Vụ góp tiền mua ma túy tại Hà Nội**: 5 viên nén màu trắng được xác định là ma túy MDMA.
3. **Vụ tổ chức sử dụng ma túy tại Sầm Sơn**: 0,686g ma túy MDMA đã bị thu giữ.
```

`must_include` của Q6 là `["Cái Quang Huy", "Lê Minh Thành", "Pháp y tâm thần"]` — không chuỗi nào xuất hiện nguyên văn.
- **Nguyên nhân:** `keyword_recall` so khớp chuỗi con chính xác; LLM tóm tắt theo tên vụ nên trúng nội dung nhưng trượt từ khóa. Đây là lỗi của **phép đo**, không phải của graph (graph có đủ person `Cái Quang Huy`, `Lê Minh Thành` và nguồn `"…Viện Pháp y tâm thần"` trong facts tổng hợp).
- **Đề xuất sửa:** thêm quy tắc vào prompt trả lời ("khi liệt kê vụ việc, nêu đầy đủ họ tên nhân vật chính và tên đơn vị như trong nguồn"), hoặc báo cáo song song judge; đánh đổi: câu trả lời dài hơn, tốn thêm output token (`out_tok` hiện 90/câu).

### Lỗi E3 (mở rộng): Case gán thừa tội danh do gộp nhiều bị cáo — Q4

- **Hiện tượng:** GraphRAG trả lời Hoàng Nato bị bắt về **cả** "tổ chức sử dụng" **và** "mua bán trái phép chất ma túy", dẫn cả Điều 251 khoản 4 lẫn Điều 255 khoản 4 → judge 1 (đúng lõi nhưng thừa).
- **Bằng chứng:** Cypher + kết quả:

```cypher
MATCH (k:Case {case_key:'dương-minh-tuấn__mua-bán-trái-phép-chất-ma-túy'})-[:CHARGED_WITH]->(c:Crime)
RETURN c.name ORDER BY c.name
```

```text
mua bán trái phép chất ma túy
tàng trữ trái phép chất ma túy
tổ chức sử dụng trái phép chất ma túy
```

Câu trả lời (dòng 29): *"…bị bắt về hành vi tổ chức sử dụng trái phép chất ma túy và mua bán trái phép chất ma túy… theo Điều 255 BLHS … và Điều 251 BLHS…"*.
- **Nguyên nhân:** bài `news-100260920221957595` tường thuật cả chuyên án 126 người với nhiều hành vi; LLM gán cả 3 tội cho một `Case` (khóa theo người chính + tội chính). `case_key` còn lấy tội sắp xếp đầu bảng chữ cái ("mua bán") nên tên khóa cũng gây hiểu nhầm.
- **Đề xuất sửa:** siết prompt trích xuất (chỉ gán tội của người chính / tách `Case` theo bị cáo), hoặc thêm node `Conviction` gắn tội theo từng người và từng giai đoạn tố tụng; đánh đổi: trích xuất nghiêm hơn có thể bỏ sót tội phụ, thêm node/chi phí.

### Lỗi E5 (đã sửa trong KG-3): ngân sách fact ưu tiên sai — Q5 lượt chạy đầu

- **Hiện tượng:** lượt `--judge` đầu, GraphRAG trả "khoản b của **Điều 249**" → recall 0.60, judge 1.
- **Bằng chứng trước/sau:** trước khi sửa, `context()` từ `news-100260917203001265` trả 60 fact nhưng fact luật bị các case tổng hợp theo MDMA ăn hết ngân sách (chỉ còn `Điều 249 --DEFINES--> tàng trữ`). Sau khi sửa (case chính đi sâu, case tổng hợp chỉ 1 dòng + nguồn), context còn 20 fact và có:

```text
Vụ vận chuyển ma túy từ Đức về Việt Nam: Điều 250 BLHS khoản 4 điểm b áp dụng cho MDMA 9600 g (có khối lượng 100 gam trở lên)
```

Lượt judge cuối: Q5 graph recall **1.00**, judge **2**, trả lời đúng "khoản 4 của Điều 250 BLHS … 20 năm, tù chung thân hoặc tử hình".
- **Nguyên nhân:** thứ tự dựng fact + `max_facts=60`: các case tổng hợp theo chất được thêm chi tiết đầy đủ trước khi tới phần luật của case chính.
- **Đề xuất sửa (đã áp dụng):** tách `primary case` (từ doc_id/tên người) và `aggregate case` (từ tên chất trong câu hỏi); chỉ case chính đi sâu; so ngưỡng theo từng case; thêm tiêu đề nguồn cho case tổng hợp. Chi phí: thêm ~1 truy vấn/lần gọi context, không thêm token đáng kể.

## 4. Kết luận (5 điểm)

- **Nên dùng KG khi:** câu hỏi cần nối thực thể giữa hai KB hoặc nhiều tài liệu, và/hoặc cần so khớp định lượng với ngưỡng luật. Bằng chứng: Q3 graph recall 1.00/judge 2 so với Flat 0.00/0; Q5 graph 1.00/2 so với Flat 0.60/1; trung bình GraphRAG recall **0.83** vs **0.43**, judge **1.67** vs **1.00**.
- **Flat RAG là đủ khi:** đáp án nằm gọn trong một tài liệu (Q1, Q2 cùng judge 2), hoặc khi ngân sách rất hạn chế: Flat rẻ hơn **4,9×** mỗi câu và indexing rẻ hơn **8,7×**.
- **Điều kiện số lượng:** với mức chênh indexing +$0,00861 và +$0,00051/câu, GraphRAG chỉ "hoàn vốn" sau khoảng **17 câu hỏi** cùng bộ dữ liệu; dưới ngưỡng đó Flat RAG hiệu quả hơn.
- **Chất lượng còn phụ thuộc trích xuất:** Q4 cho thấy graph đúng về cấu trúc nhưng kém chính xác khi một bài gộp nhiều bị cáo (judge 1); Q6 cho thấy phép đo recall chuỗi chưa phản ánh nội dung đúng (judge 1 dù recall 0). Kết luận có dẫn số liệu, không dựa cảm tính.

## 5. Tự kiểm (5 điểm)

```
$ pytest tests/ -q
................................................                         [100%]
48 passed in 0.09s

$ python bench_kg.py --check
[OK] Dữ liệu: 18 điều luật, 20 bài báo
[OK] KG-1 link_entity
[OK] Neo4j kết nối được
[provider] chat = openai:gpt-4o-mini | embedding = openai:text-embedding-3-small
[OK] KG-2 build_graph: 549 node / 1138 cạnh, đường xuyên 2 KB dài 3 cạnh
[OK] KG-3 context: 30 dữ kiện, có Điều 251
[OK] KG-4 GraphRAGAgent.answer
[OK] Chi phí check: 1 lần gọi LLM, $0.00082. ...
```

Ảnh Neo4j: `report/img/kg_count.png`, `report/img/kg_cross_kb.png`, `report/img/kg_my_case.png` — **chưa chụp (cần chụp tay trên Neo4j Browser)**; truy vấn chính xác:

```cypher
// kg_count: Q-A
MATCH (n) RETURN labels(n)[0] AS label, count(*) AS n ORDER BY n DESC;

// kg_cross_kb: Lê Minh Thành
MATCH p=(d:Document {doc_id:'news-100260918080821054'})
      <-[:DOCUMENTED_BY]-(:Case {case_key:'lê-minh-thành__mua-bán-trái-phép-chất-ma-túy'})
      -[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(:Article)
RETURN p;

// kg_my_case: Cái Quang Huy (đi hết ontology sâu)
MATCH p=(:Person {name:'Cái Quang Huy'})-[:INVOLVED_IN]->(:Case)
      -[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(:Article)
      -[:HAS_CLAUSE]->(:Clause {number:4})-[:HAS_POINT]->(:Point {letter:'b'})
      -[:HAS_THRESHOLD]->(:QuantityThreshold)-[:APPLIES_TO]->(:Substance {name:'MDMA'})
RETURN p;
```

Người đã chọn cho `kg_my_case.png`: **Cái Quang Huy** (vụ vận chuyển 9,6kg MDMA — thể hiện đúng phần ontology sâu: ngưỡng 100g → Điều 250 khoản 4 điểm b).

## Vấn đề gặp phải (không tính điểm)

- Ban đầu thiếu Docker daemon + `.env`; đã tạo container `neo4j-drug-kg` theo đúng LAB_GUIDE, chờ healthy rồi mới chạy. Sau khi có key OpenAI, `--build --limit 2` và `--check` chạy thật thành công.
- Phát hiện và sửa 1 lỗi KG-3 thật (ngân sách fact ưu tiên sai, mục 3 — E5) trước khi chạy judge cuối.
- Trích xuất LLM không tất định: giữa các lần chạy, số node/cạnh và tên Case thay đổi nhẹ (607–613 node; 1215–1220 cạnh); số liệu báo cáo bám theo `ket_qua_benchmark_kg.txt` của lần chạy cuối.
