# Sample / Golden Data

Nguon: fixtures tu tao (synthetic, anonymized) mo phong dung schema
cong khai cua Facebook Graph API, TikTok API, YouTube Data API v3.
Khong chua credential, cookie, token hay du lieu nguoi dung that.
Giay phep: dung noi bo cho phat trien/CI cua du an.

Tai tao:
    python scripts/collect_sample_data.py --out data/sample

Cau truc:
- fixtures/<platform>/video_list.raw.json  : response tho mo phong
- fixtures/<platform>/normalized.json      : sau chuan hoa
- golden/cases.json                        : golden cases cho CI
- benchmark/benchmark.csv                  : latency/success-rate parser

Golden chuan nam o data/golden/ (cay duy nhat); fixture o do la ban
sao response tho that thuoc ve parser so huu. Script nay sinh dung
shape do nen golden/ o day luon bang data/golden/ — chay lai script
khong tao drift.

Du lieu tho va du lieu chuan hoa luon tach rieng; golden case fail
nghĩa la schema/ parser da lech - phai xem lai connector truoc khi merge.
