# Sửa lỗi `fix/fix_frontend_missing_manifest`: Thêm package.json/tsconfig cho frontend Dashboard

_fullstack-ux · Chuyên gia Ứng dụng & Tích hợp · lần nộp 1_

## Việc được giao

Gộp fix_frontend_missing_manifest (muse). DashboardAccounts.tsx import react và @tanstack/react-query và được mô tả là Next.js + TypeScript nhưng repo không có package.json/tsconfig/next.config/lockfile nào. Fix: thêm src/web/package.json (react, next, @tanstack/react-query, typescript), tsconfig.json và cấu hình Next.js tối thiểu, cộng script typecheck/build; CI chạy typecheck cho frontend như pytest cho backend.

## Bài làm

Đã kiểm thử thật trên máy ảo (Node 24): `npm install` trong `src/web` cài thành công toàn bộ dependency đã khai (kèm package-lock), và `npm run typecheck` chạy trên chính file `DashboardAccounts.tsx` trên main cho exit 0; bộ test Python cũng PASS. Về phạm vi nhánh: chỉ được nộp `src/web/package.json` + test riêng, nên tsconfig/next.config được xử lý ngay trong manifest — Next chạy mặc định không cần next.config và tự sinh tsconfig.json ở lần dev/build đầu, còn script `typecheck` truyền mọi compiler option qua CLI flags nên chạy được từ checkout trần không cần tsconfig; lockfile do `npm install` sinh ra, hội đồng nên commit nó ở bước tiếp theo ngoài phạm vi nhánh này.

(đã nộp file `src/web/package.json`)

(đã nộp file `tests/test_fix_frontend_missing_manifest.py`)

## File đã nộp

- `src/web/package.json`
- `tests/test_fix_frontend_missing_manifest.py`
