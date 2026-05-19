-- ============================================================
-- AC attributes v2 — refactor air_conditioners filters
--
-- Безопасно вызывать многократно. Затрагивает ТОЛЬКО строки
-- с category_key = 'air_conditioners'. Не трогает:
--   boilers, refrigerators, washing_machines, hoods,
--   microwaves, gas_stoves.
-- ============================================================

BEGIN;

-- 1. Снимаем is_filter с устаревших полей кондиционеров.
--    Данные в products.specifications_json НЕ удаляются.
UPDATE category_attributes
   SET is_filter = FALSE
 WHERE category_key = 'air_conditioners'
   AND attr_key IN ('inverter', 'wifi', 'power');

-- 2. compressor_type: опция non_inverter → «Звичайний» / «Обычный».
UPDATE category_attributes
   SET options_json = '[
        {"value": "inverter",     "ru": "Инверторный", "uk": "Інверторний"},
        {"value": "non_inverter", "ru": "Обычный",     "uk": "Звичайний"}
       ]'::jsonb,
       is_filter = TRUE,
       sort_order = 40
 WHERE category_key = 'air_conditioners'
   AND attr_key = 'compressor_type';

-- 3. freon: label_uk → «Фреон», is_filter = TRUE.
UPDATE category_attributes
   SET label_uk = 'Фреон',
       is_filter = TRUE,
       sort_order = 50
 WHERE category_key = 'air_conditioners'
   AND attr_key = 'freon';

-- 4. room_area и energy_class остаются фильтрами.
UPDATE category_attributes
   SET is_filter = TRUE, sort_order = 10
 WHERE category_key = 'air_conditioners' AND attr_key = 'room_area';

UPDATE category_attributes
   SET is_filter = TRUE, sort_order = 70
 WHERE category_key = 'air_conditioners' AND attr_key = 'energy_class';

-- 5. Новые text-поля: добавляем (если ещё нет), is_filter = FALSE.
INSERT INTO category_attributes
    (category_key, attr_key, label_ru, label_uk, attr_type, unit,
     options_json, is_filter, sort_order)
VALUES
    ('air_conditioners', 'power_consumption',
     'Потребляемая мощность холод/тепло, Вт',
     'Споживана потужність холод/тепло, Вт',
     'text', NULL, '[]'::jsonb, FALSE, 80),
    ('air_conditioners', 'cooling_heating_capacity',
     'Производительность, кВт холод/тепло',
     'Продуктивність, кВт холод/тепло',
     'text', NULL, '[]'::jsonb, FALSE, 90),
    ('air_conditioners', 'indoor_outdoor_dimensions',
     'Размеры внутр./внешн. блока, мм',
     'Розміри внутр./зовн. блоку, мм',
     'text', NULL, '[]'::jsonb, FALSE, 100),
    ('air_conditioners', 'indoor_noise_level',
     'Уровень шума внутреннего блока, дБ',
     'Рівень шуму внутрішнього блоку, дБ',
     'number', 'дБ', '[]'::jsonb, FALSE, 110)
ON CONFLICT (category_key, attr_key) DO NOTHING;

-- 6. Если эти поля кто-то вручную пометил как фильтры — снимаем.
UPDATE category_attributes
   SET is_filter = FALSE
 WHERE category_key = 'air_conditioners'
   AND attr_key IN (
        'power_consumption',
        'cooling_heating_capacity',
        'indoor_outdoor_dimensions',
        'indoor_noise_level'
   );

COMMIT;

-- Проверка результата (для ручного запуска):
-- SELECT attr_key, label_uk, attr_type, is_filter, sort_order
--   FROM category_attributes
--  WHERE category_key = 'air_conditioners'
--  ORDER BY sort_order;
