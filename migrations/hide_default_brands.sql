-- migrations/hide_default_brands.sql
--
-- Soft-hide стандартных брендов в справочнике site_brands.
-- Записи НЕ удаляются физически — только переключается is_active = FALSE.
-- После выполнения эти бренды перестанут отображаться:
--   * в меню "Выберите бренд:" при добавлении товара
--   * в фильтре брендов на сайте
-- Уже созданные товары не пострадают: бренд хранится строкой в products.brand.
--
-- Безопасно запускать повторно: WHERE is_active = TRUE гарантирует idempotency.

UPDATE site_brands
SET is_active = FALSE
WHERE LOWER(TRIM(name)) IN (
    'samsung',
    'lg',
    'bosch',
    'beko',
    'philips',
    'xiaomi'
)
AND is_active = TRUE;

-- Проверка результата (необязательно):
-- SELECT id, name, is_active FROM site_brands ORDER BY is_active DESC, LOWER(name);
