--
-- PostgreSQL database dump
--


-- Dumped from database version 14.24 (Ubuntu 14.24-0ubuntu0.22.04.1)
-- Dumped by pg_dump version 14.24 (Ubuntu 14.24-0ubuntu0.22.04.1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: core; Type: SCHEMA; Schema: -; Owner: deskon_app
--

CREATE SCHEMA core;



--
-- Name: review; Type: SCHEMA; Schema: -; Owner: deskon_app
--

CREATE SCHEMA review;



--
-- Name: staging; Type: SCHEMA; Schema: -; Owner: deskon_app
--

CREATE SCHEMA staging;



SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: claim_diagnoses; Type: TABLE; Schema: core; Owner: deskon_app
--

CREATE TABLE core.claim_diagnoses (
    id bigint NOT NULL,
    claim_id bigint NOT NULL,
    sequence_no integer NOT NULL,
    diagnosis_code character varying(50) NOT NULL,
    diagnosis_name text,
    source_text text,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);



--
-- Name: claim_diagnoses_id_seq; Type: SEQUENCE; Schema: core; Owner: deskon_app
--

ALTER TABLE core.claim_diagnoses ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME core.claim_diagnoses_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: claim_documents; Type: TABLE; Schema: core; Owner: deskon_app
--

CREATE TABLE core.claim_documents (
    id bigint NOT NULL,
    claim_id bigint NOT NULL,
    document_type character varying(100) NOT NULL,
    document_name character varying(255),
    embed_url text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_by bigint NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);



--
-- Name: TABLE claim_documents; Type: COMMENT; Schema: core; Owner: deskon_app
--

COMMENT ON TABLE core.claim_documents IS 'Document metadata and external/embed URL. Actual PDF/document content is not stored in the database.';


--
-- Name: claim_documents_id_seq; Type: SEQUENCE; Schema: core; Owner: deskon_app
--

ALTER TABLE core.claim_documents ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME core.claim_documents_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: claim_procedures; Type: TABLE; Schema: core; Owner: deskon_app
--

CREATE TABLE core.claim_procedures (
    id bigint NOT NULL,
    claim_id bigint NOT NULL,
    sequence_no integer NOT NULL,
    procedure_code character varying(100) NOT NULL,
    procedure_name text,
    source_text text,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);



--
-- Name: claim_procedures_id_seq; Type: SEQUENCE; Schema: core; Owner: deskon_app
--

ALTER TABLE core.claim_procedures ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME core.claim_procedures_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: claim_topups; Type: TABLE; Schema: core; Owner: deskon_app
--

CREATE TABLE core.claim_topups (
    id bigint NOT NULL,
    claim_id bigint NOT NULL,
    topup_type character varying(10) NOT NULL,
    topup_code character varying(50),
    topup_description text,
    topup_tariff numeric(18,2),
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_claim_topups_type CHECK (((topup_type)::text = ANY ((ARRAY['SA'::character varying, 'SD'::character varying, 'SI'::character varying, 'SP'::character varying, 'SR'::character varying])::text[])))
);



--
-- Name: TABLE claim_topups; Type: COMMENT; Schema: core; Owner: deskon_app
--

COMMENT ON TABLE core.claim_topups IS 'Optional claim top-up components derived from Kdsa/Kdsd/Kdsi/Kdsp/Kdsr and their descriptions/tariffs.';


--
-- Name: claim_topups_id_seq; Type: SEQUENCE; Schema: core; Owner: deskon_app
--

ALTER TABLE core.claim_topups ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME core.claim_topups_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: claims; Type: TABLE; Schema: core; Owner: deskon_app
--

CREATE TABLE core.claims (
    id bigint NOT NULL,
    nosjp character varying(100) NOT NULL,
    hospital_id bigint NOT NULL,
    claim_status character varying(50) NOT NULL,
    nokapst character varying(100),
    kdppklayan character varying(50) NOT NULL,
    nmtkp character varying(50) NOT NULL,
    service_month date NOT NULL,
    tgldtgsep date,
    tglplgsep date,
    jenis_kelamin character varying(20),
    umur_tahun character varying(50),
    klsrawat character varying(50),
    kdinacbgs character varying(50),
    nminacbgs character varying(255),
    kddiagprimer character varying(50),
    nmdiagprimer text,
    nmdokter character varying(255),
    politujsep character varying(255),
    nmdati2layan character varying(255),
    nmppklayan character varying(255),
    nmjnspulang character varying(255),
    severity_level character varying(50),
    biayars numeric(18,2),
    bytagsep numeric(18,2),
    tarifgrup numeric(18,2),
    no_bast character varying(100),
    no_surat_bast character varying(100),
    tgl_bast date,
    flag_biometrik boolean,
    flag_iterasi boolean,
    source_import_batch_id bigint,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_claims_status CHECK (((claim_status)::text = ANY ((ARRAY['PENDING'::character varying, 'VERIFIKASI_PASCA_KLAIM'::character varying, 'AUDIT_ADMINISTRASI_KLAIM'::character varying])::text[])))
);



--
-- Name: TABLE claims; Type: COMMENT; Schema: core; Owner: deskon_app
--

COMMENT ON TABLE core.claims IS 'Canonical claim data. Grain: one valid Nosjp per claim.';


--
-- Name: COLUMN claims.nosjp; Type: COMMENT; Schema: core; Owner: deskon_app
--

COMMENT ON COLUMN core.claims.nosjp IS 'Business key / unique claim number.';


--
-- Name: COLUMN claims.nokapst; Type: COMMENT; Schema: core; Owner: deskon_app
--

COMMENT ON COLUMN core.claims.nokapst IS 'Participant card identifier. Development/source data is masked or dummy.';


--
-- Name: COLUMN claims.kdppklayan; Type: COMMENT; Schema: core; Owner: deskon_app
--

COMMENT ON COLUMN core.claims.kdppklayan IS 'FKRTL code used to associate claim access with hospital.';


--
-- Name: COLUMN claims.nmtkp; Type: COMMENT; Schema: core; Owner: deskon_app
--

COMMENT ON COLUMN core.claims.nmtkp IS 'Service level: RJTL or RITL.';


--
-- Name: COLUMN claims.service_month; Type: COMMENT; Schema: core; Owner: deskon_app
--

COMMENT ON COLUMN core.claims.service_month IS 'Service month derived from Tglpelayanan. Source format YYYY/MM/DD with DD always 01.';


--
-- Name: claims_id_seq; Type: SEQUENCE; Schema: core; Owner: deskon_app
--

ALTER TABLE core.claims ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME core.claims_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: hospitals; Type: TABLE; Schema: core; Owner: deskon_app
--

CREATE TABLE core.hospitals (
    id bigint NOT NULL,
    kdppklayan character varying(50) NOT NULL,
    hospital_name character varying(255) NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);



--
-- Name: hospitals_id_seq; Type: SEQUENCE; Schema: core; Owner: deskon_app
--

ALTER TABLE core.hospitals ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME core.hospitals_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: users; Type: TABLE; Schema: core; Owner: deskon_app
--

CREATE TABLE core.users (
    id bigint NOT NULL,
    full_name character varying(255) NOT NULL,
    email character varying(255) NOT NULL,
    password_hash text NOT NULL,
    role character varying(50) NOT NULL,
    hospital_id bigint,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_users_role CHECK (((role)::text = ANY ((ARRAY['BPJS_VERIFIER'::character varying, 'HOSPITAL_USER'::character varying, 'ADMIN'::character varying])::text[])))
);



--
-- Name: users_id_seq; Type: SEQUENCE; Schema: core; Owner: deskon_app
--

ALTER TABLE core.users ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME core.users_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: claim_reselections; Type: TABLE; Schema: review; Owner: deskon_app
--

CREATE TABLE review.claim_reselections (
    id bigint NOT NULL,
    claim_id bigint NOT NULL,
    review_id bigint NOT NULL,
    target_type character varying(50) NOT NULL,
    action character varying(50) NOT NULL,
    original_code character varying(100) NOT NULL,
    original_description text,
    proposed_code character varying(100),
    proposed_description text,
    reason text NOT NULL,
    status character varying(50) DEFAULT 'DRAFT'::character varying NOT NULL,
    created_by bigint NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    agreed_by bigint,
    agreed_at timestamp with time zone,
    corrects_reselection_id bigint,
    CONSTRAINT ck_reselections_action CHECK (((action)::text = ANY ((ARRAY['CHANGE'::character varying, 'DROP'::character varying])::text[]))),
    CONSTRAINT ck_reselections_status CHECK (((status)::text = ANY ((ARRAY['DRAFT'::character varying, 'PROPOSED'::character varying, 'AGREED'::character varying, 'REJECTED'::character varying, 'CORRECTED'::character varying, 'CANCELLED'::character varying])::text[]))),
    CONSTRAINT ck_reselections_target_type CHECK (((target_type)::text = ANY ((ARRAY['DIAGNOSIS'::character varying, 'PROCEDURE'::character varying])::text[])))
);



--
-- Name: claim_reselections_id_seq; Type: SEQUENCE; Schema: review; Owner: deskon_app
--

ALTER TABLE review.claim_reselections ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME review.claim_reselections_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: claim_reviews; Type: TABLE; Schema: review; Owner: deskon_app
--

CREATE TABLE review.claim_reviews (
    id bigint NOT NULL,
    claim_id bigint NOT NULL,
    status character varying(50) DEFAULT 'OPEN'::character varying NOT NULL,
    final_decision character varying(50),
    resolution_note text,
    opened_by bigint NOT NULL,
    opened_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    closed_by bigint,
    closed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_claim_reviews_closed_data CHECK (((((status)::text = 'OPEN'::text) AND (final_decision IS NULL) AND (closed_by IS NULL) AND (closed_at IS NULL)) OR (((status)::text = 'CLOSED'::text) AND (final_decision IS NOT NULL) AND (closed_by IS NOT NULL) AND (closed_at IS NOT NULL)))),
    CONSTRAINT ck_claim_reviews_final_decision CHECK (((final_decision IS NULL) OR ((final_decision)::text = ANY ((ARRAY['LAYAK'::character varying, 'TIDAK_LAYAK'::character varying, 'RESELEKSI'::character varying])::text[])))),
    CONSTRAINT ck_claim_reviews_status CHECK (((status)::text = ANY ((ARRAY['OPEN'::character varying, 'CLOSED'::character varying])::text[])))
);



--
-- Name: claim_reviews_id_seq; Type: SEQUENCE; Schema: review; Owner: deskon_app
--

ALTER TABLE review.claim_reviews ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME review.claim_reviews_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: preliminary_notes; Type: TABLE; Schema: review; Owner: deskon_app
--

CREATE TABLE review.preliminary_notes (
    id bigint NOT NULL,
    claim_id bigint NOT NULL,
    note text NOT NULL,
    created_by bigint NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_preliminary_notes_note CHECK ((TRIM(BOTH FROM note) <> ''::text))
);



--
-- Name: preliminary_notes_id_seq; Type: SEQUENCE; Schema: review; Owner: deskon_app
--

ALTER TABLE review.preliminary_notes ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME review.preliminary_notes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: review_comments; Type: TABLE; Schema: review; Owner: deskon_app
--

CREATE TABLE review.review_comments (
    id bigint NOT NULL,
    review_id bigint NOT NULL,
    user_id bigint NOT NULL,
    parent_comment_id bigint,
    comment_text text NOT NULL,
    status character varying(50) DEFAULT 'DRAFT'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    published_at timestamp with time zone,
    corrects_comment_id bigint,
    CONSTRAINT ck_review_comments_status CHECK (((status)::text = ANY ((ARRAY['DRAFT'::character varying, 'PUBLISHED'::character varying, 'CORRECTED'::character varying, 'CANCELLED'::character varying])::text[])))
);



--
-- Name: review_comments_id_seq; Type: SEQUENCE; Schema: review; Owner: deskon_app
--

ALTER TABLE review.review_comments ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME review.review_comments_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: review_events; Type: TABLE; Schema: review; Owner: deskon_app
--

CREATE TABLE review.review_events (
    id bigint NOT NULL,
    review_id bigint NOT NULL,
    user_id bigint NOT NULL,
    event_type character varying(100) NOT NULL,
    entity_type character varying(100),
    entity_id bigint,
    event_data jsonb,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);



--
-- Name: TABLE review_events; Type: COMMENT; Schema: review; Owner: deskon_app
--

COMMENT ON TABLE review.review_events IS 'Append-only audit trail of DESKON review activities.';


--
-- Name: review_events_id_seq; Type: SEQUENCE; Schema: review; Owner: deskon_app
--

ALTER TABLE review.review_events ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME review.review_events_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: review_findings; Type: TABLE; Schema: review; Owner: deskon_app
--

CREATE TABLE review.review_findings (
    id bigint NOT NULL,
    review_id bigint NOT NULL,
    finding_category character varying(100) NOT NULL,
    finding_title character varying(255) NOT NULL,
    finding_description text NOT NULL,
    status character varying(50) DEFAULT 'DRAFT'::character varying NOT NULL,
    created_by bigint NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    published_by bigint,
    published_at timestamp with time zone,
    corrects_finding_id bigint,
    CONSTRAINT ck_review_findings_status CHECK (((status)::text = ANY ((ARRAY['DRAFT'::character varying, 'PUBLISHED'::character varying, 'CORRECTED'::character varying, 'CANCELLED'::character varying])::text[])))
);



--
-- Name: review_findings_id_seq; Type: SEQUENCE; Schema: review; Owner: deskon_app
--

ALTER TABLE review.review_findings ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME review.review_findings_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: import_batches; Type: TABLE; Schema: staging; Owner: deskon_app
--

CREATE TABLE staging.import_batches (
    id bigint NOT NULL,
    source_filename character varying(500) NOT NULL,
    source_type character varying(50) NOT NULL,
    imported_by bigint NOT NULL,
    total_rows integer DEFAULT 0 NOT NULL,
    valid_rows integer DEFAULT 0 NOT NULL,
    invalid_rows integer DEFAULT 0 NOT NULL,
    status character varying(50) DEFAULT 'UPLOADED'::character varying NOT NULL,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_import_batches_source_type CHECK (((source_type)::text = ANY ((ARRAY['CSV'::character varying, 'TXT'::character varying, 'XLSX'::character varying, 'OTHER'::character varying])::text[]))),
    CONSTRAINT ck_import_batches_status CHECK (((status)::text = ANY ((ARRAY['UPLOADED'::character varying, 'VALIDATING'::character varying, 'VALIDATED'::character varying, 'TRANSFORMING'::character varying, 'IMPORTED'::character varying, 'FAILED'::character varying])::text[])))
);



--
-- Name: import_batches_id_seq; Type: SEQUENCE; Schema: staging; Owner: deskon_app
--

ALTER TABLE staging.import_batches ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME staging.import_batches_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: staging_claims; Type: TABLE; Schema: staging; Owner: deskon_app
--

CREATE TABLE staging.staging_claims (
    id bigint NOT NULL,
    import_batch_id bigint NOT NULL,
    source_row_number integer,
    nosjp character varying(100) NOT NULL,
    source_claim_status text,
    biayars numeric(18,2),
    bytagsep numeric(18,2),
    kddiagprimer character varying(50),
    nmdiagprimer text,
    diagsekunder text,
    prosedur text,
    flag_biometrik boolean,
    flag_iterasi boolean,
    jenis_kelamin character varying(20),
    kdinacbgs character varying(50),
    nminacbgs character varying(255),
    kdsa character varying(50),
    kdsd character varying(50),
    kdsi character varying(50),
    kdsp character varying(50),
    kdsr character varying(50),
    klsrawat character varying(50),
    kdppklayan character varying(50),
    nmppklayan character varying(255),
    nmdati2layan character varying(255),
    nmdokter character varying(255),
    nmjnspulang character varying(255),
    nmtkp character varying(50),
    no_bast character varying(100),
    no_surat_bast character varying(100),
    nokapst character varying(100),
    politujsep character varying(255),
    severity_level character varying(50),
    tarifgrup numeric(18,2),
    tarifsa numeric(18,2),
    tarifsd numeric(18,2),
    tarifsi numeric(18,2),
    tarifsp numeric(18,2),
    tarifsr numeric(18,2),
    tgl_bast date,
    tgldtgsep date,
    tglpelayanan date,
    tglplgsep date,
    umur_tahun text,
    validation_status character varying(50) DEFAULT 'PENDING'::character varying NOT NULL,
    validation_message text,
    raw_payload jsonb,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT ck_staging_claims_validation_status CHECK (((validation_status)::text = ANY ((ARRAY['PENDING'::character varying, 'VALID'::character varying, 'INVALID'::character varying])::text[])))
);



--
-- Name: staging_claims_id_seq; Type: SEQUENCE; Schema: staging; Owner: deskon_app
--

ALTER TABLE staging.staging_claims ALTER COLUMN id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME staging.staging_claims_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: claim_diagnoses claim_diagnoses_pkey; Type: CONSTRAINT; Schema: core; Owner: deskon_app
--

ALTER TABLE ONLY core.claim_diagnoses
    ADD CONSTRAINT claim_diagnoses_pkey PRIMARY KEY (id);


--
-- Name: claim_documents claim_documents_pkey; Type: CONSTRAINT; Schema: core; Owner: deskon_app
--

ALTER TABLE ONLY core.claim_documents
    ADD CONSTRAINT claim_documents_pkey PRIMARY KEY (id);


--
-- Name: claim_procedures claim_procedures_pkey; Type: CONSTRAINT; Schema: core; Owner: deskon_app
--

ALTER TABLE ONLY core.claim_procedures
    ADD CONSTRAINT claim_procedures_pkey PRIMARY KEY (id);


--
-- Name: claim_topups claim_topups_pkey; Type: CONSTRAINT; Schema: core; Owner: deskon_app
--

ALTER TABLE ONLY core.claim_topups
    ADD CONSTRAINT claim_topups_pkey PRIMARY KEY (id);


--
-- Name: claims claims_pkey; Type: CONSTRAINT; Schema: core; Owner: deskon_app
--

ALTER TABLE ONLY core.claims
    ADD CONSTRAINT claims_pkey PRIMARY KEY (id);


--
-- Name: hospitals hospitals_pkey; Type: CONSTRAINT; Schema: core; Owner: deskon_app
--

ALTER TABLE ONLY core.hospitals
    ADD CONSTRAINT hospitals_pkey PRIMARY KEY (id);


--
-- Name: claim_diagnoses uq_claim_diagnoses_sequence; Type: CONSTRAINT; Schema: core; Owner: deskon_app
--

ALTER TABLE ONLY core.claim_diagnoses
    ADD CONSTRAINT uq_claim_diagnoses_sequence UNIQUE (claim_id, sequence_no);


--
-- Name: claim_procedures uq_claim_procedures_sequence; Type: CONSTRAINT; Schema: core; Owner: deskon_app
--

ALTER TABLE ONLY core.claim_procedures
    ADD CONSTRAINT uq_claim_procedures_sequence UNIQUE (claim_id, sequence_no);


--
-- Name: claim_topups uq_claim_topups_type; Type: CONSTRAINT; Schema: core; Owner: deskon_app
--

ALTER TABLE ONLY core.claim_topups
    ADD CONSTRAINT uq_claim_topups_type UNIQUE (claim_id, topup_type);


--
-- Name: claims uq_claims_nosjp; Type: CONSTRAINT; Schema: core; Owner: deskon_app
--

ALTER TABLE ONLY core.claims
    ADD CONSTRAINT uq_claims_nosjp UNIQUE (nosjp);


--
-- Name: hospitals uq_hospitals_kdppklayan; Type: CONSTRAINT; Schema: core; Owner: deskon_app
--

ALTER TABLE ONLY core.hospitals
    ADD CONSTRAINT uq_hospitals_kdppklayan UNIQUE (kdppklayan);


--
-- Name: users uq_users_email; Type: CONSTRAINT; Schema: core; Owner: deskon_app
--

ALTER TABLE ONLY core.users
    ADD CONSTRAINT uq_users_email UNIQUE (email);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: core; Owner: deskon_app
--

ALTER TABLE ONLY core.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: claim_reselections claim_reselections_pkey; Type: CONSTRAINT; Schema: review; Owner: deskon_app
--

ALTER TABLE ONLY review.claim_reselections
    ADD CONSTRAINT claim_reselections_pkey PRIMARY KEY (id);


--
-- Name: claim_reviews claim_reviews_pkey; Type: CONSTRAINT; Schema: review; Owner: deskon_app
--

ALTER TABLE ONLY review.claim_reviews
    ADD CONSTRAINT claim_reviews_pkey PRIMARY KEY (id);


--
-- Name: preliminary_notes preliminary_notes_pkey; Type: CONSTRAINT; Schema: review; Owner: deskon_app
--

ALTER TABLE ONLY review.preliminary_notes
    ADD CONSTRAINT preliminary_notes_pkey PRIMARY KEY (id);


--
-- Name: review_comments review_comments_pkey; Type: CONSTRAINT; Schema: review; Owner: deskon_app
--

ALTER TABLE ONLY review.review_comments
    ADD CONSTRAINT review_comments_pkey PRIMARY KEY (id);


--
-- Name: review_events review_events_pkey; Type: CONSTRAINT; Schema: review; Owner: deskon_app
--

ALTER TABLE ONLY review.review_events
    ADD CONSTRAINT review_events_pkey PRIMARY KEY (id);


--
-- Name: review_findings review_findings_pkey; Type: CONSTRAINT; Schema: review; Owner: deskon_app
--

ALTER TABLE ONLY review.review_findings
    ADD CONSTRAINT review_findings_pkey PRIMARY KEY (id);


--
-- Name: import_batches import_batches_pkey; Type: CONSTRAINT; Schema: staging; Owner: deskon_app
--

ALTER TABLE ONLY staging.import_batches
    ADD CONSTRAINT import_batches_pkey PRIMARY KEY (id);


--
-- Name: staging_claims staging_claims_pkey; Type: CONSTRAINT; Schema: staging; Owner: deskon_app
--

ALTER TABLE ONLY staging.staging_claims
    ADD CONSTRAINT staging_claims_pkey PRIMARY KEY (id);


--
-- Name: idx_claim_diagnoses_claim; Type: INDEX; Schema: core; Owner: deskon_app
--

CREATE INDEX idx_claim_diagnoses_claim ON core.claim_diagnoses USING btree (claim_id);


--
-- Name: idx_claim_documents_claim; Type: INDEX; Schema: core; Owner: deskon_app
--

CREATE INDEX idx_claim_documents_claim ON core.claim_documents USING btree (claim_id);


--
-- Name: idx_claim_procedures_claim; Type: INDEX; Schema: core; Owner: deskon_app
--

CREATE INDEX idx_claim_procedures_claim ON core.claim_procedures USING btree (claim_id);


--
-- Name: idx_claim_topups_claim; Type: INDEX; Schema: core; Owner: deskon_app
--

CREATE INDEX idx_claim_topups_claim ON core.claim_topups USING btree (claim_id);


--
-- Name: idx_claims_hospital; Type: INDEX; Schema: core; Owner: deskon_app
--

CREATE INDEX idx_claims_hospital ON core.claims USING btree (hospital_id);


--
-- Name: idx_claims_kdppklayan; Type: INDEX; Schema: core; Owner: deskon_app
--

CREATE INDEX idx_claims_kdppklayan ON core.claims USING btree (kdppklayan);


--
-- Name: idx_claims_nmtkp; Type: INDEX; Schema: core; Owner: deskon_app
--

CREATE INDEX idx_claims_nmtkp ON core.claims USING btree (nmtkp);


--
-- Name: idx_claims_no_bast; Type: INDEX; Schema: core; Owner: deskon_app
--

CREATE INDEX idx_claims_no_bast ON core.claims USING btree (no_bast);


--
-- Name: idx_claims_service_month; Type: INDEX; Schema: core; Owner: deskon_app
--

CREATE INDEX idx_claims_service_month ON core.claims USING btree (service_month);


--
-- Name: idx_claims_status; Type: INDEX; Schema: core; Owner: deskon_app
--

CREATE INDEX idx_claims_status ON core.claims USING btree (claim_status);


--
-- Name: idx_claim_reviews_claim; Type: INDEX; Schema: review; Owner: deskon_app
--

CREATE INDEX idx_claim_reviews_claim ON review.claim_reviews USING btree (claim_id);


--
-- Name: idx_claim_reviews_status; Type: INDEX; Schema: review; Owner: deskon_app
--

CREATE INDEX idx_claim_reviews_status ON review.claim_reviews USING btree (status);


--
-- Name: idx_preliminary_notes_claim; Type: INDEX; Schema: review; Owner: deskon_app
--

CREATE INDEX idx_preliminary_notes_claim ON review.preliminary_notes USING btree (claim_id);


--
-- Name: idx_preliminary_notes_created_at; Type: INDEX; Schema: review; Owner: deskon_app
--

CREATE INDEX idx_preliminary_notes_created_at ON review.preliminary_notes USING btree (created_at);


--
-- Name: idx_preliminary_notes_created_by; Type: INDEX; Schema: review; Owner: deskon_app
--

CREATE INDEX idx_preliminary_notes_created_by ON review.preliminary_notes USING btree (created_by);


--
-- Name: idx_reselections_claim; Type: INDEX; Schema: review; Owner: deskon_app
--

CREATE INDEX idx_reselections_claim ON review.claim_reselections USING btree (claim_id);


--
-- Name: idx_reselections_review; Type: INDEX; Schema: review; Owner: deskon_app
--

CREATE INDEX idx_reselections_review ON review.claim_reselections USING btree (review_id);


--
-- Name: idx_review_comments_parent; Type: INDEX; Schema: review; Owner: deskon_app
--

CREATE INDEX idx_review_comments_parent ON review.review_comments USING btree (parent_comment_id);


--
-- Name: idx_review_comments_review; Type: INDEX; Schema: review; Owner: deskon_app
--

CREATE INDEX idx_review_comments_review ON review.review_comments USING btree (review_id);


--
-- Name: idx_review_events_created_at; Type: INDEX; Schema: review; Owner: deskon_app
--

CREATE INDEX idx_review_events_created_at ON review.review_events USING btree (created_at);


--
-- Name: idx_review_events_entity; Type: INDEX; Schema: review; Owner: deskon_app
--

CREATE INDEX idx_review_events_entity ON review.review_events USING btree (entity_type, entity_id);


--
-- Name: idx_review_events_review; Type: INDEX; Schema: review; Owner: deskon_app
--

CREATE INDEX idx_review_events_review ON review.review_events USING btree (review_id);


--
-- Name: idx_review_findings_review; Type: INDEX; Schema: review; Owner: deskon_app
--

CREATE INDEX idx_review_findings_review ON review.review_findings USING btree (review_id);


--
-- Name: idx_review_findings_status; Type: INDEX; Schema: review; Owner: deskon_app
--

CREATE INDEX idx_review_findings_status ON review.review_findings USING btree (status);


--
-- Name: uq_claim_reviews_open; Type: INDEX; Schema: review; Owner: deskon_app
--

CREATE UNIQUE INDEX uq_claim_reviews_open ON review.claim_reviews USING btree (claim_id) WHERE ((status)::text = 'OPEN'::text);


--
-- Name: claim_diagnoses fk_claim_diagnoses_claim; Type: FK CONSTRAINT; Schema: core; Owner: deskon_app
--

ALTER TABLE ONLY core.claim_diagnoses
    ADD CONSTRAINT fk_claim_diagnoses_claim FOREIGN KEY (claim_id) REFERENCES core.claims(id) ON DELETE RESTRICT;


--
-- Name: claim_documents fk_claim_documents_claim; Type: FK CONSTRAINT; Schema: core; Owner: deskon_app
--

ALTER TABLE ONLY core.claim_documents
    ADD CONSTRAINT fk_claim_documents_claim FOREIGN KEY (claim_id) REFERENCES core.claims(id) ON DELETE RESTRICT;


--
-- Name: claim_documents fk_claim_documents_user; Type: FK CONSTRAINT; Schema: core; Owner: deskon_app
--

ALTER TABLE ONLY core.claim_documents
    ADD CONSTRAINT fk_claim_documents_user FOREIGN KEY (created_by) REFERENCES core.users(id);


--
-- Name: claim_procedures fk_claim_procedures_claim; Type: FK CONSTRAINT; Schema: core; Owner: deskon_app
--

ALTER TABLE ONLY core.claim_procedures
    ADD CONSTRAINT fk_claim_procedures_claim FOREIGN KEY (claim_id) REFERENCES core.claims(id) ON DELETE RESTRICT;


--
-- Name: claim_topups fk_claim_topups_claim; Type: FK CONSTRAINT; Schema: core; Owner: deskon_app
--

ALTER TABLE ONLY core.claim_topups
    ADD CONSTRAINT fk_claim_topups_claim FOREIGN KEY (claim_id) REFERENCES core.claims(id) ON DELETE RESTRICT;


--
-- Name: claims fk_claims_hospital; Type: FK CONSTRAINT; Schema: core; Owner: deskon_app
--

ALTER TABLE ONLY core.claims
    ADD CONSTRAINT fk_claims_hospital FOREIGN KEY (hospital_id) REFERENCES core.hospitals(id);


--
-- Name: claims fk_claims_import_batch; Type: FK CONSTRAINT; Schema: core; Owner: deskon_app
--

ALTER TABLE ONLY core.claims
    ADD CONSTRAINT fk_claims_import_batch FOREIGN KEY (source_import_batch_id) REFERENCES staging.import_batches(id);


--
-- Name: users fk_users_hospital; Type: FK CONSTRAINT; Schema: core; Owner: deskon_app
--

ALTER TABLE ONLY core.users
    ADD CONSTRAINT fk_users_hospital FOREIGN KEY (hospital_id) REFERENCES core.hospitals(id);


--
-- Name: claim_reviews fk_claim_reviews_claim; Type: FK CONSTRAINT; Schema: review; Owner: deskon_app
--

ALTER TABLE ONLY review.claim_reviews
    ADD CONSTRAINT fk_claim_reviews_claim FOREIGN KEY (claim_id) REFERENCES core.claims(id) ON DELETE RESTRICT;


--
-- Name: claim_reviews fk_claim_reviews_closed_by; Type: FK CONSTRAINT; Schema: review; Owner: deskon_app
--

ALTER TABLE ONLY review.claim_reviews
    ADD CONSTRAINT fk_claim_reviews_closed_by FOREIGN KEY (closed_by) REFERENCES core.users(id);


--
-- Name: claim_reviews fk_claim_reviews_opened_by; Type: FK CONSTRAINT; Schema: review; Owner: deskon_app
--

ALTER TABLE ONLY review.claim_reviews
    ADD CONSTRAINT fk_claim_reviews_opened_by FOREIGN KEY (opened_by) REFERENCES core.users(id);


--
-- Name: preliminary_notes fk_preliminary_notes_claim; Type: FK CONSTRAINT; Schema: review; Owner: deskon_app
--

ALTER TABLE ONLY review.preliminary_notes
    ADD CONSTRAINT fk_preliminary_notes_claim FOREIGN KEY (claim_id) REFERENCES core.claims(id) ON DELETE RESTRICT;


--
-- Name: preliminary_notes fk_preliminary_notes_user; Type: FK CONSTRAINT; Schema: review; Owner: deskon_app
--

ALTER TABLE ONLY review.preliminary_notes
    ADD CONSTRAINT fk_preliminary_notes_user FOREIGN KEY (created_by) REFERENCES core.users(id) ON DELETE RESTRICT;


--
-- Name: claim_reselections fk_reselections_agreed_by; Type: FK CONSTRAINT; Schema: review; Owner: deskon_app
--

ALTER TABLE ONLY review.claim_reselections
    ADD CONSTRAINT fk_reselections_agreed_by FOREIGN KEY (agreed_by) REFERENCES core.users(id);


--
-- Name: claim_reselections fk_reselections_claim; Type: FK CONSTRAINT; Schema: review; Owner: deskon_app
--

ALTER TABLE ONLY review.claim_reselections
    ADD CONSTRAINT fk_reselections_claim FOREIGN KEY (claim_id) REFERENCES core.claims(id) ON DELETE RESTRICT;


--
-- Name: claim_reselections fk_reselections_correction; Type: FK CONSTRAINT; Schema: review; Owner: deskon_app
--

ALTER TABLE ONLY review.claim_reselections
    ADD CONSTRAINT fk_reselections_correction FOREIGN KEY (corrects_reselection_id) REFERENCES review.claim_reselections(id);


--
-- Name: claim_reselections fk_reselections_created_by; Type: FK CONSTRAINT; Schema: review; Owner: deskon_app
--

ALTER TABLE ONLY review.claim_reselections
    ADD CONSTRAINT fk_reselections_created_by FOREIGN KEY (created_by) REFERENCES core.users(id);


--
-- Name: claim_reselections fk_reselections_review; Type: FK CONSTRAINT; Schema: review; Owner: deskon_app
--

ALTER TABLE ONLY review.claim_reselections
    ADD CONSTRAINT fk_reselections_review FOREIGN KEY (review_id) REFERENCES review.claim_reviews(id) ON DELETE RESTRICT;


--
-- Name: review_comments fk_review_comments_correction; Type: FK CONSTRAINT; Schema: review; Owner: deskon_app
--

ALTER TABLE ONLY review.review_comments
    ADD CONSTRAINT fk_review_comments_correction FOREIGN KEY (corrects_comment_id) REFERENCES review.review_comments(id);


--
-- Name: review_comments fk_review_comments_parent; Type: FK CONSTRAINT; Schema: review; Owner: deskon_app
--

ALTER TABLE ONLY review.review_comments
    ADD CONSTRAINT fk_review_comments_parent FOREIGN KEY (parent_comment_id) REFERENCES review.review_comments(id);


--
-- Name: review_comments fk_review_comments_review; Type: FK CONSTRAINT; Schema: review; Owner: deskon_app
--

ALTER TABLE ONLY review.review_comments
    ADD CONSTRAINT fk_review_comments_review FOREIGN KEY (review_id) REFERENCES review.claim_reviews(id) ON DELETE RESTRICT;


--
-- Name: review_comments fk_review_comments_user; Type: FK CONSTRAINT; Schema: review; Owner: deskon_app
--

ALTER TABLE ONLY review.review_comments
    ADD CONSTRAINT fk_review_comments_user FOREIGN KEY (user_id) REFERENCES core.users(id);


--
-- Name: review_events fk_review_events_review; Type: FK CONSTRAINT; Schema: review; Owner: deskon_app
--

ALTER TABLE ONLY review.review_events
    ADD CONSTRAINT fk_review_events_review FOREIGN KEY (review_id) REFERENCES review.claim_reviews(id) ON DELETE RESTRICT;


--
-- Name: review_events fk_review_events_user; Type: FK CONSTRAINT; Schema: review; Owner: deskon_app
--

ALTER TABLE ONLY review.review_events
    ADD CONSTRAINT fk_review_events_user FOREIGN KEY (user_id) REFERENCES core.users(id);


--
-- Name: review_findings fk_review_findings_correction; Type: FK CONSTRAINT; Schema: review; Owner: deskon_app
--

ALTER TABLE ONLY review.review_findings
    ADD CONSTRAINT fk_review_findings_correction FOREIGN KEY (corrects_finding_id) REFERENCES review.review_findings(id);


--
-- Name: review_findings fk_review_findings_created_by; Type: FK CONSTRAINT; Schema: review; Owner: deskon_app
--

ALTER TABLE ONLY review.review_findings
    ADD CONSTRAINT fk_review_findings_created_by FOREIGN KEY (created_by) REFERENCES core.users(id);


--
-- Name: review_findings fk_review_findings_published_by; Type: FK CONSTRAINT; Schema: review; Owner: deskon_app
--

ALTER TABLE ONLY review.review_findings
    ADD CONSTRAINT fk_review_findings_published_by FOREIGN KEY (published_by) REFERENCES core.users(id);


--
-- Name: review_findings fk_review_findings_review; Type: FK CONSTRAINT; Schema: review; Owner: deskon_app
--

ALTER TABLE ONLY review.review_findings
    ADD CONSTRAINT fk_review_findings_review FOREIGN KEY (review_id) REFERENCES review.claim_reviews(id) ON DELETE RESTRICT;


--
-- Name: import_batches fk_import_batches_user; Type: FK CONSTRAINT; Schema: staging; Owner: deskon_app
--

ALTER TABLE ONLY staging.import_batches
    ADD CONSTRAINT fk_import_batches_user FOREIGN KEY (imported_by) REFERENCES core.users(id);


--
-- Name: staging_claims fk_staging_claims_batch; Type: FK CONSTRAINT; Schema: staging; Owner: deskon_app
--

ALTER TABLE ONLY staging.staging_claims
    ADD CONSTRAINT fk_staging_claims_batch FOREIGN KEY (import_batch_id) REFERENCES staging.import_batches(id);


--
-- PostgreSQL database dump complete
--


