ALTER TABLE scholight.surveys
    ADD COLUMN title TEXT;

ALTER TABLE scholight.surveys
    ADD CONSTRAINT surveys_title_length CHECK (
        title IS NULL OR char_length(btrim(title)) BETWEEN 1 AND 160
    );
