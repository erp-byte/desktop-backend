-- ============================================================================
-- supabase_schema_all_tables.sql   (GENERATED — do not hand-edit)
-- Full DDL for the Candor warehouse schema (all NON-legacy tables), produced by
-- pg_dump --schema-only of a clean from-scratch build (114 files, 0 errors) on
-- Postgres 16. The 5 v1 "legacy" job-card tables are excluded.
-- Run this once on a fresh Supabase database to replicate the schema.
-- Source components are in ../db/ (out-of-band types/tables + drop-legacy); the
-- canonical SFG migrations are app/db/050_sfg_foundation.sql + 053_sfg_box.sql.
-- ============================================================================
--
-- PostgreSQL database dump
--

\restrict zZLSf7xTEIOc3Wy4WNmRSMJ7krOCfVr4YcjgLcYQ9hgP0xIffbc297vvLpy0QFk

-- Dumped from database version 16.14
-- Dumped by pg_dump version 16.14

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
-- Name: e_extraction_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.e_extraction_status AS ENUM (
    'pending',
    'extracted',
    'failed',
    'error'
);


--
-- Name: fn_sync_phase_batch_id(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.fn_sync_phase_batch_id() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF NEW.batch_id IS NOT NULL AND NEW.phase_id IS NULL THEN
        NEW.phase_id := NEW.batch_id;
    ELSIF NEW.phase_id IS NOT NULL AND NEW.batch_id IS NULL THEN
        NEW.batch_id := NEW.phase_id;
    ELSIF NEW.batch_id IS NOT NULL
       AND NEW.phase_id IS NOT NULL
       AND NEW.batch_id <> NEW.phase_id
    THEN
        RAISE EXCEPTION
            'batch_id (%) and phase_id (%) must match — caller passed both',
            NEW.batch_id, NEW.phase_id;
    END IF;
    RETURN NEW;
END;
$$;


--
-- Name: fn_touch_jcvar_last_updated(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.fn_touch_jcvar_last_updated() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.last_updated_at := NOW();
    RETURN NEW;
END;
$$;


--
-- Name: fn_touch_updated_at(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.fn_touch_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;


--
-- Name: vendor_history_block_update(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.vendor_history_block_update() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    RAISE EXCEPTION 'vendor history tables are append-only (UPDATE blocked on %)', TG_TABLE_NAME;
END;
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: ai_recommendation; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_recommendation (
    recommendation_id integer NOT NULL,
    recommendation_type text NOT NULL,
    entity text,
    prompt_text text,
    response_text text,
    response_json jsonb,
    tokens_used integer,
    latency_ms integer,
    model_used text,
    status text DEFAULT 'generated'::text NOT NULL,
    feedback text,
    plan_id integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ai_recommendation_entity_check CHECK ((entity = ANY (ARRAY['cfpl'::text, 'cdpl'::text])))
);


--
-- Name: ai_recommendation_recommendation_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ai_recommendation_recommendation_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ai_recommendation_recommendation_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ai_recommendation_recommendation_id_seq OWNED BY public.ai_recommendation.recommendation_id;


--
-- Name: all_sku; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.all_sku (
    sku_id integer NOT NULL,
    particulars text NOT NULL,
    item_type text,
    item_group text,
    sub_group text,
    uom numeric(15,3),
    sale_group text,
    gst numeric(15,3),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    batch_strategy text DEFAULT 'FIFO'::text,
    min_shelf_life_days integer DEFAULT 0,
    CONSTRAINT all_sku_batch_strategy_check CHECK ((batch_strategy = ANY (ARRAY['FIFO'::text, 'FEFO'::text])))
);


--
-- Name: COLUMN all_sku.item_type; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.all_sku.item_type IS 'rm | pm | fg | sfg';


--
-- Name: all_sku_sku_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.all_sku_sku_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: all_sku_sku_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.all_sku_sku_id_seq OWNED BY public.all_sku.sku_id;


--
-- Name: amendment_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.amendment_log (
    id integer NOT NULL,
    record_id text NOT NULL,
    record_type text NOT NULL,
    field_name text NOT NULL,
    previous_value text,
    new_value text,
    changed_by text NOT NULL,
    changed_at timestamp with time zone DEFAULT now() NOT NULL,
    reason text
);


--
-- Name: amendment_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.amendment_log_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: amendment_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.amendment_log_id_seq OWNED BY public.amendment_log.id;


--
-- Name: auth_refresh_token; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auth_refresh_token (
    jti uuid NOT NULL,
    user_id integer NOT NULL,
    parent_jti uuid,
    chain_root uuid NOT NULL,
    issued_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    rotated_at timestamp with time zone,
    revoked_at timestamp with time zone,
    revoke_reason text,
    ip text,
    user_agent text,
    device_info jsonb
);


--
-- Name: auth_active_sessions; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.auth_active_sessions AS
 SELECT jti AS token_id,
    user_id,
    issued_at,
    expires_at,
    ip,
    user_agent,
    device_info,
    chain_root
   FROM public.auth_refresh_token
  WHERE ((revoked_at IS NULL) AND (rotated_at IS NULL) AND (expires_at > now()));


--
-- Name: auth_password_reset_otp; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auth_password_reset_otp (
    user_id integer NOT NULL,
    otp_hash text NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_phone text
);


--
-- Name: auth_permission; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auth_permission (
    permission_id integer NOT NULL,
    module text NOT NULL,
    sub_module text,
    sub_sub_module text,
    action text NOT NULL,
    description text
);


--
-- Name: auth_permission_permission_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.auth_permission_permission_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: auth_permission_permission_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.auth_permission_permission_id_seq OWNED BY public.auth_permission.permission_id;


--
-- Name: auth_role; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auth_role (
    role_id integer NOT NULL,
    role_name text NOT NULL,
    description text,
    is_admin boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: auth_role_permission; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auth_role_permission (
    role_id integer NOT NULL,
    permission_id integer NOT NULL,
    allowed_entities text[],
    allowed_warehouses text[],
    allowed_floors text[]
);


--
-- Name: auth_role_role_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.auth_role_role_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: auth_role_role_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.auth_role_role_id_seq OWNED BY public.auth_role.role_id;


--
-- Name: auth_session; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auth_session (
    session_id integer NOT NULL,
    user_id integer NOT NULL,
    token text NOT NULL,
    ip_address text,
    user_agent text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    last_activity_at timestamp with time zone DEFAULT now() NOT NULL,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: auth_session_session_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.auth_session_session_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: auth_session_session_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.auth_session_session_id_seq OWNED BY public.auth_session.session_id;


--
-- Name: auth_user; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auth_user (
    user_id integer NOT NULL,
    phone text NOT NULL,
    password_encrypted text NOT NULL,
    full_name text NOT NULL,
    email text,
    role_id integer,
    entity text,
    allowed_warehouses text[],
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_login_at timestamp with time zone,
    allowed_floors text[],
    allowed_entities text[],
    status text DEFAULT 'active'::text NOT NULL,
    failed_login_count integer DEFAULT 0 NOT NULL,
    locked_until timestamp with time zone,
    must_change_password boolean DEFAULT false NOT NULL,
    password_changed_at timestamp with time zone,
    CONSTRAINT auth_user_status_check CHECK ((status = ANY (ARRAY['active'::text, 'suspended'::text, 'disabled'::text])))
);


--
-- Name: auth_user_role; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.auth_user_role (
    user_id integer NOT NULL,
    role_id integer NOT NULL
);


--
-- Name: auth_user_user_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.auth_user_user_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: auth_user_user_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.auth_user_user_id_seq OWNED BY public.auth_user.user_id;


--
-- Name: batch_block_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.batch_block_history (
    id integer NOT NULL,
    batch_id text NOT NULL,
    action text NOT NULL,
    so_id integer,
    blocked_by text,
    override_by text,
    override_note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: batch_block_history_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.batch_block_history_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: batch_block_history_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.batch_block_history_id_seq OWNED BY public.batch_block_history.id;


--
-- Name: batch_rejection_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.batch_rejection_log (
    log_id integer NOT NULL,
    batch_id text NOT NULL,
    rejected_by text NOT NULL,
    rejected_at timestamp with time zone DEFAULT now() NOT NULL,
    reason_code text NOT NULL,
    reason_text text,
    job_card_id integer,
    so_id integer,
    entity text,
    CONSTRAINT batch_rejection_log_entity_check CHECK ((entity = ANY (ARRAY['cfpl'::text, 'cdpl'::text])))
);


--
-- Name: batch_rejection_log_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.batch_rejection_log_log_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: batch_rejection_log_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.batch_rejection_log_log_id_seq OWNED BY public.batch_rejection_log.log_id;


--
-- Name: bom_amendment_request_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bom_amendment_request_v2 (
    request_id bigint NOT NULL,
    request_type text NOT NULL,
    bom_id integer,
    job_card_id bigint,
    payload_jsonb jsonb NOT NULL,
    maker_user_id integer,
    checker1_user_id integer,
    checker2_user_id integer,
    status text DEFAULT 'draft'::text NOT NULL,
    reason text,
    rejection_reason text,
    applied_version integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    decided_at timestamp with time zone,
    applied_at timestamp with time zone,
    checker1_note text,
    checker2_note text,
    consumed_at timestamp with time zone,
    CONSTRAINT bom_amendment_request_v2_request_type_check CHECK ((request_type = ANY (ARRAY['one_off_material_add'::text, 'one_off_material_remove'::text, 'permanent_bom_add'::text, 'permanent_bom_remove'::text, 'permanent_bom_qty_change'::text, 'plan_delete'::text, 'stop_process'::text, 'force_unlock'::text, 'uom_correction'::text, 'ncr_disposition'::text, 'unbalanced_close_override'::text, 'ega_override'::text]))),
    CONSTRAINT bom_amendment_request_v2_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'pending_review'::text, 'pending_final'::text, 'approved'::text, 'applied'::text, 'rejected'::text, 'withdrawn'::text])))
);


--
-- Name: COLUMN bom_amendment_request_v2.payload_jsonb; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.bom_amendment_request_v2.payload_jsonb IS 'Canonical payload shape per request_type:
  one_off_material_add      {"material_sku_name": text, "qty_kg": numeric, "uom": text, "reason"?: text}
  one_off_material_remove   {"material_sku_name": text, "reason"?: text}
  permanent_bom_add         {"material_sku_name": text, "qty_per_unit": numeric, "uom": text, "loss_pct"?: numeric, "item_type": "rm"|"pm"}
  permanent_bom_remove      {"bom_line_id": int}
  permanent_bom_qty_change  {"bom_line_id": int, "old_qty": numeric, "new_qty": numeric}
  plan_delete               {"plan_id": int}
  stop_process              {"reason": text}
  force_unlock              {"reason": text}
  uom_correction            {"sku_id": int, "old_uom_kg": numeric, "new_uom_kg": numeric}
  ncr_disposition           {"output_id": int, "disposition": "scrap"|"rework"|"accept_concession", "reason": text}
  unbalanced_close_override {"balance_diff_kg": numeric, "reason": text}
  ega_override              {"material_sku_name": text, "ega_kg": numeric, "reason": text}
Validated app-side via Pydantic; DB enforces request_type only.';


--
-- Name: COLUMN bom_amendment_request_v2.consumed_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.bom_amendment_request_v2.consumed_at IS 'Set by the downstream consumer (B5 force-close, stop_process, force_unlock, ega_override byproducts-save, unbalanced_close_override JC /complete) when an approved noop-apply amendment is redeemed. NULL means the token is still available for redemption.';


--
-- Name: bom_header; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bom_header (
    bom_id integer NOT NULL,
    fg_sku_name text NOT NULL,
    customer_name text,
    pack_size_kg numeric(15,3),
    version integer DEFAULT 1 NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    effective_from date,
    effective_to date,
    item_group text,
    entity text,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    sub_group text,
    process_category text,
    business_unit text,
    factory text,
    floors text[],
    machines text[],
    shelf_life_days integer,
    gst_rate numeric(5,3),
    hsn_sac text,
    inventory_group text,
    customer_code text,
    output_uom text DEFAULT 'kg'::text,
    allowed_balance_tolerance_pct numeric(5,4) DEFAULT 0.001 NOT NULL,
    bar_line_process text,
    CONSTRAINT bom_header_entity_check CHECK ((entity = ANY (ARRAY['cfpl'::text, 'cdpl'::text])))
);


--
-- Name: COLUMN bom_header.allowed_balance_tolerance_pct; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.bom_header.allowed_balance_tolerance_pct IS 'R9 closure-gate tolerance: abs(balance_diff) / total_input_qty must be <= this to allow PUT job-cards-v2 id complete. Default 0.001 (0.1 pct).';


--
-- Name: bom_header_bom_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.bom_header_bom_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: bom_header_bom_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.bom_header_bom_id_seq OWNED BY public.bom_header.bom_id;


--
-- Name: bom_line; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bom_line (
    bom_line_id integer NOT NULL,
    bom_id integer NOT NULL,
    line_number integer NOT NULL,
    material_sku_name text NOT NULL,
    item_type text NOT NULL,
    quantity_per_unit numeric(15,3) NOT NULL,
    uom text,
    loss_pct numeric(5,3) DEFAULT 0,
    godown text,
    can_use_offgrade boolean DEFAULT false,
    offgrade_max_pct numeric(5,3) DEFAULT 0,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    unit_rate_inr numeric(15,3),
    process_stage text,
    staging_method text DEFAULT 'pick'::text,
    consumed_at_stage text,
    CONSTRAINT bom_line_staging_method_check CHECK ((staging_method = ANY (ARRAY['pick'::text, 'backflush'::text, 'floor_stock'::text])))
);


--
-- Name: COLUMN bom_line.item_type; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.bom_line.item_type IS 'rm | pm | fg | sfg';


--
-- Name: bom_line_bom_line_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.bom_line_bom_line_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: bom_line_bom_line_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.bom_line_bom_line_id_seq OWNED BY public.bom_line.bom_line_id;


--
-- Name: bom_process_route; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.bom_process_route (
    route_id integer NOT NULL,
    bom_id integer NOT NULL,
    step_number integer NOT NULL,
    process_name text NOT NULL,
    stage text NOT NULL,
    std_time_min numeric(10,2),
    loss_pct numeric(5,3) DEFAULT 0,
    qc_check text,
    machine_type text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    practical_operation text,
    stage_bucket text,
    input_kind text,
    output_kind text,
    input_code text,
    output_code text
);


--
-- Name: bom_process_route_route_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.bom_process_route_route_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: bom_process_route_route_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.bom_process_route_route_id_seq OWNED BY public.bom_process_route.route_id;


--
-- Name: cascade_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cascade_events (
    event_id integer NOT NULL,
    batch_id text NOT NULL,
    old_so_id integer,
    new_so_id integer,
    old_indent_id integer,
    new_indent_id integer,
    executed_by text,
    executed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: cascade_events_event_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.cascade_events_event_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: cascade_events_event_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.cascade_events_event_id_seq OWNED BY public.cascade_events.event_id;


--
-- Name: coa_document; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.coa_document (
    coa_id text NOT NULL,
    transaction_no text,
    line_number integer,
    uploaded_at timestamp with time zone,
    dock_intimation_id integer,
    qc_intimation_id integer,
    sku_id integer,
    sku_name_raw text,
    supplier_id integer,
    lot_number text,
    s3_key text,
    file_name text,
    file_size_bytes bigint,
    mime_type text,
    scan_status text DEFAULT 'pending'::text NOT NULL,
    coa_status text DEFAULT 'active'::text NOT NULL,
    replaces_coa_id text,
    replaced_at timestamp with time zone,
    replaced_reason text,
    replaced_by text,
    deleted_at timestamp with time zone,
    deleted_by text,
    delete_reason text,
    remarks text,
    entity text,
    CONSTRAINT coa_document_anchor_present CHECK (((transaction_no IS NOT NULL) OR (dock_intimation_id IS NOT NULL) OR (qc_intimation_id IS NOT NULL) OR (sku_id IS NOT NULL) OR (sku_name_raw IS NOT NULL))),
    CONSTRAINT coa_document_coa_status_check CHECK ((coa_status = ANY (ARRAY['active'::text, 'superseded'::text, 'deleted'::text]))),
    CONSTRAINT coa_document_deleted_consistency CHECK (((coa_status = 'deleted'::text) = (deleted_at IS NOT NULL))),
    CONSTRAINT coa_document_scan_status_check CHECK ((scan_status = ANY (ARRAY['pending'::text, 'clean'::text, 'infected'::text, 'skipped'::text])))
);


--
-- Name: day_end_balance_scan; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.day_end_balance_scan (
    scan_id integer NOT NULL,
    floor_location text NOT NULL,
    scan_date date NOT NULL,
    submitted_by text,
    submitted_at timestamp with time zone,
    reviewed_by text,
    reviewed_at timestamp with time zone,
    total_system_qty numeric(15,3),
    total_scanned_qty numeric(15,3),
    total_variance numeric(15,3),
    status text DEFAULT 'pending'::text NOT NULL,
    entity text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT day_end_balance_scan_entity_check CHECK ((entity = ANY (ARRAY['cfpl'::text, 'cdpl'::text])))
);


--
-- Name: day_end_balance_scan_line; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.day_end_balance_scan_line (
    scan_line_id integer NOT NULL,
    scan_id integer NOT NULL,
    sku_name text NOT NULL,
    item_type text,
    system_qty_kg numeric(15,3),
    scanned_qty_kg numeric(15,3),
    variance_kg numeric(15,3),
    variance_pct numeric(5,3),
    scanned_box_ids text[],
    variance_reason text,
    corrective_action text,
    status text DEFAULT 'ok'::text NOT NULL
);


--
-- Name: day_end_balance_scan_line_scan_line_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.day_end_balance_scan_line_scan_line_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: day_end_balance_scan_line_scan_line_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.day_end_balance_scan_line_scan_line_id_seq OWNED BY public.day_end_balance_scan_line.scan_line_id;


--
-- Name: day_end_balance_scan_scan_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.day_end_balance_scan_scan_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: day_end_balance_scan_scan_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.day_end_balance_scan_scan_id_seq OWNED BY public.day_end_balance_scan.scan_id;


--
-- Name: discrepancy_report; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.discrepancy_report (
    discrepancy_id integer NOT NULL,
    discrepancy_type text NOT NULL,
    severity text DEFAULT 'major'::text NOT NULL,
    affected_material text,
    affected_machine_id integer,
    affected_job_card_ids integer[],
    affected_plan_line_ids integer[],
    details text,
    total_affected_qty_kg numeric(15,3),
    customer_impact text,
    resolution_type text,
    resolution_details text,
    reported_by text,
    reported_at timestamp with time zone DEFAULT now() NOT NULL,
    resolved_by text,
    resolved_at timestamp with time zone,
    status text DEFAULT 'open'::text NOT NULL,
    entity text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT discrepancy_report_entity_check CHECK ((entity = ANY (ARRAY['cfpl'::text, 'cdpl'::text])))
);


--
-- Name: discrepancy_report_discrepancy_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.discrepancy_report_discrepancy_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: discrepancy_report_discrepancy_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.discrepancy_report_discrepancy_id_seq OWNED BY public.discrepancy_report.discrepancy_id;


--
-- Name: fifo_skip_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fifo_skip_log (
    id integer NOT NULL,
    batch_id text NOT NULL,
    job_card_id text,
    reason text NOT NULL,
    detail text,
    disposition text,
    block_for_so text,
    skipped_by text NOT NULL,
    skipped_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT fifo_skip_log_disposition_check CHECK ((disposition = ANY (ARRAY['leave_available'::text, 'block_for_so'::text, 'hold'::text, 'quarantine'::text, 'reject'::text])))
);


--
-- Name: fifo_skip_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.fifo_skip_log_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: fifo_skip_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.fifo_skip_log_id_seq OWNED BY public.fifo_skip_log.id;


--
-- Name: floor_inventory; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.floor_inventory (
    inventory_id integer NOT NULL,
    sku_name text NOT NULL,
    item_type text,
    floor_location text NOT NULL,
    quantity_kg numeric(15,3) DEFAULT 0 NOT NULL,
    lot_number text,
    entity text,
    last_updated timestamp with time zone DEFAULT now() NOT NULL,
    uom text DEFAULT 'kg'::text,
    CONSTRAINT floor_inventory_entity_check CHECK ((entity = ANY (ARRAY['cfpl'::text, 'cdpl'::text])))
);


--
-- Name: floor_inventory_inventory_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.floor_inventory_inventory_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: floor_inventory_inventory_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.floor_inventory_inventory_id_seq OWNED BY public.floor_inventory.inventory_id;


--
-- Name: floor_movement; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.floor_movement (
    movement_id integer NOT NULL,
    sku_name text NOT NULL,
    from_location text NOT NULL,
    to_location text NOT NULL,
    quantity_kg numeric(15,3) NOT NULL,
    reason text,
    job_card_id integer,
    scanned_qr_codes text[],
    entity text,
    moved_by text,
    moved_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT floor_movement_entity_check CHECK ((entity = ANY (ARRAY['cfpl'::text, 'cdpl'::text])))
);


--
-- Name: floor_movement_movement_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.floor_movement_movement_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: floor_movement_movement_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.floor_movement_movement_id_seq OWNED BY public.floor_movement.movement_id;


--
-- Name: fulfillment_bom_override; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fulfillment_bom_override (
    override_id integer NOT NULL,
    fulfillment_id integer NOT NULL,
    bom_line_id integer,
    material_sku_name text,
    quantity_per_unit numeric(15,3),
    loss_pct numeric(5,3),
    uom text,
    godown text,
    is_removed boolean DEFAULT false NOT NULL,
    override_reason text,
    overridden_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: fulfillment_bom_override_override_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.fulfillment_bom_override_override_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: fulfillment_bom_override_override_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.fulfillment_bom_override_override_id_seq OWNED BY public.fulfillment_bom_override.override_id;


--
-- Name: fulfillment_bom_override_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fulfillment_bom_override_v2 (
    override_id bigint NOT NULL,
    so_fulfillment_id bigint NOT NULL,
    bom_line_id integer,
    material_sku_name text,
    quantity_per_unit numeric(15,3),
    loss_pct numeric(5,3),
    uom text,
    godown text,
    is_removed boolean DEFAULT false NOT NULL,
    override_reason text,
    overridden_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE fulfillment_bom_override_v2; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.fulfillment_bom_override_v2 IS 'v2 per-fulfillment BOM-line overrides. NULL bom_line_id = added item not in master BOM.';


--
-- Name: fulfillment_bom_override_v2_override_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.fulfillment_bom_override_v2_override_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: fulfillment_bom_override_v2_override_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.fulfillment_bom_override_v2_override_id_seq OWNED BY public.fulfillment_bom_override_v2.override_id;


--
-- Name: fulfillment_floor_stock; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fulfillment_floor_stock (
    floor_stock_id integer NOT NULL,
    fulfillment_id integer NOT NULL,
    material_sku_name text NOT NULL,
    item_type text DEFAULT 'pm'::text,
    quantity_kg numeric(15,3) NOT NULL,
    unit text DEFAULT 'KG'::text NOT NULL,
    floor_location text NOT NULL,
    added_by text,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: fulfillment_floor_stock_floor_stock_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.fulfillment_floor_stock_floor_stock_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: fulfillment_floor_stock_floor_stock_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.fulfillment_floor_stock_floor_stock_id_seq OWNED BY public.fulfillment_floor_stock.floor_stock_id;


--
-- Name: fulfillment_floor_stock_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.fulfillment_floor_stock_v2 (
    floor_stock_id bigint NOT NULL,
    so_fulfillment_id bigint NOT NULL,
    material_sku_name text NOT NULL,
    item_type text DEFAULT 'pm'::text,
    quantity_kg numeric(15,3) NOT NULL,
    unit text DEFAULT 'KG'::text NOT NULL,
    floor_location text NOT NULL,
    added_by text,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: TABLE fulfillment_floor_stock_v2; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.fulfillment_floor_stock_v2 IS 'v2 manual floor-stock entries per fulfillment (material lying on production floor).';


--
-- Name: fulfillment_floor_stock_v2_floor_stock_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.fulfillment_floor_stock_v2_floor_stock_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: fulfillment_floor_stock_v2_floor_stock_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.fulfillment_floor_stock_v2_floor_stock_id_seq OWNED BY public.fulfillment_floor_stock_v2.floor_stock_id;


--
-- Name: gate_pass_sample_details; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.gate_pass_sample_details (
    gate_pass_id integer NOT NULL,
    requisition_id integer NOT NULL,
    original_requisition_id integer,
    sample_type text,
    purpose_tag text,
    purpose_note text,
    converted_from_internal boolean DEFAULT false NOT NULL,
    conversion_qty numeric,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: gate_passes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.gate_passes (
    id integer NOT NULL,
    gate_pass_number text NOT NULL,
    gate_pass_type text DEFAULT 'SAMPLE'::text NOT NULL,
    source_ref_type text,
    source_ref_id integer,
    material_document_id integer,
    from_location text,
    to_location text,
    recipient_name text,
    recipient_contact text,
    vehicle_carrier text,
    driver_name text,
    issued_by integer NOT NULL,
    issued_at timestamp with time zone DEFAULT now() NOT NULL,
    approver1_user_id integer,
    approver1_at timestamp with time zone,
    approver2_user_id integer,
    approver2_at timestamp with time zone,
    print_count integer DEFAULT 0 NOT NULL,
    last_printed_at timestamp with time zone,
    voided boolean DEFAULT false NOT NULL,
    voided_at timestamp with time zone,
    voided_by integer,
    void_reason text,
    warehouse text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT gate_passes_gate_pass_type_check CHECK ((gate_pass_type = ANY (ARRAY['SAMPLE'::text, 'TRANSFER'::text, 'OTHER'::text]))),
    CONSTRAINT gate_passes_warehouse_check CHECK ((warehouse = ANY (ARRAY['W202'::text, 'A185'::text, 'A68'::text, 'F53'::text, 'A101'::text, 'D-39'::text, 'D-514'::text, 'Rishi'::text, 'Supreme'::text])))
);


--
-- Name: gate_passes_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.gate_passes_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: gate_passes_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.gate_passes_id_seq OWNED BY public.gate_passes.id;


--
-- Name: inter_entity_transfer; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.inter_entity_transfer (
    id integer NOT NULL,
    transfer_id text NOT NULL,
    from_entity text NOT NULL,
    to_entity text NOT NULL,
    transfer_date date DEFAULT CURRENT_DATE NOT NULL,
    status text DEFAULT 'dispatched'::text NOT NULL,
    dispatched_by text,
    dispatched_at timestamp with time zone,
    received_by text,
    received_at timestamp with time zone,
    mat_doc_dispatch text,
    mat_doc_receipt text,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT inter_entity_transfer_from_entity_check CHECK ((from_entity = ANY (ARRAY['cfpl'::text, 'cdpl'::text]))),
    CONSTRAINT inter_entity_transfer_status_check CHECK ((status = ANY (ARRAY['dispatched'::text, 'in_transit'::text, 'received'::text, 'cancelled'::text]))),
    CONSTRAINT inter_entity_transfer_to_entity_check CHECK ((to_entity = ANY (ARRAY['cfpl'::text, 'cdpl'::text])))
);


--
-- Name: inter_entity_transfer_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.inter_entity_transfer_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: inter_entity_transfer_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.inter_entity_transfer_id_seq OWNED BY public.inter_entity_transfer.id;


--
-- Name: inter_entity_transfer_line; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.inter_entity_transfer_line (
    id integer NOT NULL,
    transfer_id text NOT NULL,
    batch_id text NOT NULL,
    sku_name text NOT NULL,
    quantity_kg numeric(15,3) NOT NULL,
    lot_number text,
    dispatched_qty_kg numeric(15,3),
    received_qty_kg numeric(15,3)
);


--
-- Name: inter_entity_transfer_line_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.inter_entity_transfer_line_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: inter_entity_transfer_line_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.inter_entity_transfer_line_id_seq OWNED BY public.inter_entity_transfer_line.id;


--
-- Name: internal_issue_note; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.internal_issue_note (
    note_id integer NOT NULL,
    note_number text NOT NULL,
    sku_name text NOT NULL,
    batch_id text,
    quantity_kg numeric(15,3) NOT NULL,
    source_warehouse text,
    source_floor text,
    destination_floor text NOT NULL,
    purpose text NOT NULL,
    requested_by text NOT NULL,
    approved_by text,
    approved_at timestamp with time zone,
    status text DEFAULT 'pending'::text NOT NULL,
    entity text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    is_space_constrained boolean DEFAULT false,
    reject_reason text,
    CONSTRAINT internal_issue_note_entity_check CHECK ((entity = ANY (ARRAY['cfpl'::text, 'cdpl'::text])))
);


--
-- Name: internal_issue_note_note_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.internal_issue_note_note_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: internal_issue_note_note_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.internal_issue_note_note_id_seq OWNED BY public.internal_issue_note.note_id;


--
-- Name: internal_order; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.internal_order (
    id integer NOT NULL,
    internal_order_id text NOT NULL,
    prod_indent_id text,
    item_description text NOT NULL,
    material_type text NOT NULL,
    required_qty numeric(12,2),
    status text DEFAULT 'created'::text NOT NULL,
    entity text DEFAULT 'cfpl'::text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    completed_at timestamp with time zone,
    CONSTRAINT internal_order_entity_check CHECK ((entity = ANY (ARRAY['cfpl'::text, 'cdpl'::text]))),
    CONSTRAINT internal_order_status_check CHECK ((status = ANY (ARRAY['created'::text, 'jc_assigned'::text, 'in_progress'::text, 'completed'::text, 'cancelled'::text])))
);


--
-- Name: internal_order_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.internal_order_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: internal_order_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.internal_order_id_seq OWNED BY public.internal_order.id;


--
-- Name: interunit_transfer_boxes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.interunit_transfer_boxes (
    id bigint NOT NULL,
    header_id bigint,
    box_id text,
    lot_number text,
    net_weight numeric(15,3)
);


--
-- Name: interunit_transfer_in_boxes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.interunit_transfer_in_boxes (
    id bigint NOT NULL,
    header_id bigint,
    box_id text,
    is_matched boolean,
    lot_number text
);


--
-- Name: interunit_transfer_in_header; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.interunit_transfer_in_header (
    id bigint NOT NULL,
    transfer_out_id bigint,
    grn_number text,
    receiving_warehouse text,
    status text
);


--
-- Name: interunit_transfers_header; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.interunit_transfers_header (
    id bigint NOT NULL,
    challan_no text,
    from_site text,
    to_site text,
    request_id bigint,
    status text
);


--
-- Name: interunit_transfers_lines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.interunit_transfers_lines (
    id bigint NOT NULL,
    header_id bigint,
    item_category text,
    qty numeric(15,3)
);


--
-- Name: inventory_batch; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.inventory_batch (
    batch_id text NOT NULL,
    sku_name text NOT NULL,
    item_type text,
    transaction_no text,
    lot_number text,
    source text DEFAULT 'INWARD'::text NOT NULL,
    inward_date date NOT NULL,
    manufacturing_date date,
    expiry_date date,
    original_qty_kg numeric(15,3) NOT NULL,
    current_qty_kg numeric(15,3) NOT NULL,
    warehouse_id text,
    floor_id text DEFAULT 'rm_store'::text,
    status text DEFAULT 'AVAILABLE'::text NOT NULL,
    blocked_for_so_id integer,
    blocked_by text,
    blocked_at timestamp with time zone,
    block_reason text,
    flag_reason text,
    flag_detail text,
    ownership text DEFAULT 'FLOOR'::text NOT NULL,
    entity text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT inventory_batch_entity_check CHECK ((entity = ANY (ARRAY['cfpl'::text, 'cdpl'::text])))
);


--
-- Name: inventory_event_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.inventory_event_log (
    event_id integer NOT NULL,
    batch_id text NOT NULL,
    event_type text NOT NULL,
    from_status text,
    to_status text,
    from_location text,
    to_location text,
    quantity_kg numeric(15,3),
    reference_type text,
    reference_id integer,
    so_id integer,
    performed_by text,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: inventory_event_log_event_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.inventory_event_log_event_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: inventory_event_log_event_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.inventory_event_log_event_id_seq OWNED BY public.inventory_event_log.event_id;


--
-- Name: issue_note; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.issue_note (
    id integer NOT NULL,
    issue_note_id text NOT NULL,
    job_card_id text NOT NULL,
    so_id text,
    customer_name text,
    bom_line_id text,
    issued_by text NOT NULL,
    issued_at timestamp with time zone DEFAULT now(),
    status text DEFAULT 'draft'::text NOT NULL,
    reservation_expires_at timestamp with time zone,
    total_weight_kg numeric(12,3) DEFAULT 0,
    entity text DEFAULT 'cfpl'::text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    movement_type text DEFAULT '261'::text,
    CONSTRAINT issue_note_entity_check CHECK ((entity = ANY (ARRAY['cfpl'::text, 'cdpl'::text]))),
    CONSTRAINT issue_note_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'confirmed'::text, 'partially_reversed'::text, 'reversed'::text])))
);


--
-- Name: issue_note_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.issue_note_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: issue_note_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.issue_note_id_seq OWNED BY public.issue_note.id;


--
-- Name: issue_note_line; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.issue_note_line (
    id integer NOT NULL,
    issue_note_id text NOT NULL,
    bom_line_id text,
    sku text,
    material_type text,
    lot_number text,
    lot_id text,
    tr_number text,
    warehouse text,
    net_wt_issued numeric(12,3) NOT NULL,
    qty_cartons integer,
    box_id text,
    fifo_skipped boolean DEFAULT false,
    skip_reason text,
    movement_type text DEFAULT '261'::text
);


--
-- Name: issue_note_line_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.issue_note_line_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: issue_note_line_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.issue_note_line_id_seq OWNED BY public.issue_note_line.id;


--
-- Name: jc_material_exception_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.jc_material_exception_v2 (
    exception_id bigint NOT NULL,
    job_card_id bigint NOT NULL,
    request_id bigint NOT NULL,
    material_sku_name text NOT NULL,
    qty_kg numeric(15,3),
    qty_pcs numeric(15,3),
    exception_type text NOT NULL,
    reason text,
    status text DEFAULT 'applied'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT jc_material_exception_v2_exception_type_check CHECK ((exception_type = ANY (ARRAY['one_off_add'::text, 'one_off_remove'::text])))
);


--
-- Name: job_card_accounting_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_card_accounting_v2 (
    accounting_id bigint NOT NULL,
    job_card_id bigint NOT NULL,
    total_input_qty numeric(15,3) NOT NULL,
    input_uom text NOT NULL,
    output_qty numeric(15,3) DEFAULT 0 NOT NULL,
    output_uom text,
    output_qty_units numeric(15,3),
    output_kind text NOT NULL,
    carried_in_qty numeric(15,3) DEFAULT 0 NOT NULL,
    dispatched_out_qty numeric(15,3) DEFAULT 0 NOT NULL,
    process_loss_qty numeric(15,3) DEFAULT 0 NOT NULL,
    process_loss_breakdown jsonb DEFAULT '{}'::jsonb NOT NULL,
    extra_give_away_qty numeric(15,3) DEFAULT 0 NOT NULL,
    balance_material_qty numeric(15,3) DEFAULT 0 NOT NULL,
    offgrade_total_qty numeric(15,3) DEFAULT 0 NOT NULL,
    rejection_qty numeric(15,3) DEFAULT 0 NOT NULL,
    wastage_qty numeric(15,3) DEFAULT 0 NOT NULL,
    control_sample_qty numeric(15,3) DEFAULT 0 NOT NULL,
    total_accounted_qty numeric(15,3) DEFAULT 0 NOT NULL,
    balance_difference_qty numeric(15,3) DEFAULT 0 NOT NULL,
    is_balanced boolean DEFAULT false NOT NULL,
    process_loss_pct numeric(6,3),
    other_loss_pct numeric(6,3),
    total_loss_pct numeric(6,3),
    saved_by text,
    saved_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone,
    invisible_loss_pct numeric(6,3),
    pm_variance_breakdown jsonb DEFAULT '{}'::jsonb NOT NULL,
    batch_id bigint,
    CONSTRAINT job_card_accounting_v2_carried_in_qty_check CHECK ((carried_in_qty >= (0)::numeric)),
    CONSTRAINT job_card_accounting_v2_dispatched_out_qty_check CHECK ((dispatched_out_qty >= (0)::numeric)),
    CONSTRAINT job_card_accounting_v2_input_uom_check CHECK ((input_uom = ANY (ARRAY['KGS'::text, 'GMS'::text, 'LTRS'::text, 'NOS'::text, 'PCS'::text, 'ROLL'::text, 'SETS'::text, 'BUNDLE'::text]))),
    CONSTRAINT job_card_accounting_v2_output_kind_check CHECK ((output_kind = ANY (ARRAY['SFG'::text, 'WIP'::text, 'FG'::text]))),
    CONSTRAINT job_card_accounting_v2_output_qty_check CHECK ((output_qty >= (0)::numeric)),
    CONSTRAINT job_card_accounting_v2_output_uom_check CHECK ((output_uom = ANY (ARRAY['KGS'::text, 'GMS'::text, 'LTRS'::text, 'NOS'::text, 'PCS'::text, 'ROLL'::text, 'SETS'::text, 'BUNDLE'::text]))),
    CONSTRAINT job_card_accounting_v2_total_input_qty_check CHECK ((total_input_qty >= (0)::numeric))
);


--
-- Name: COLUMN job_card_accounting_v2.invisible_loss_pct; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.job_card_accounting_v2.invisible_loss_pct IS 'R9 process_loss_pct + ega_loss_pct. Persisted so dashboards can index without re-summing.';


--
-- Name: COLUMN job_card_accounting_v2.pm_variance_breakdown; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.job_card_accounting_v2.pm_variance_breakdown IS 'R11 PM variance roll-up. Canonical shape:
  {"pm_torn": <pcs>, "pm_damaged": <pcs>, "pm_misprint": <pcs>, "pm_rejection": <pcs>, "pm_wasted": <pcs>}
Each key optional; missing key implies 0 pcs in that bucket. JSONB so future PM categories can be added without a migration.';


--
-- Name: job_card_additive_consumption_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_card_additive_consumption_v2 (
    additive_id bigint NOT NULL,
    job_card_id bigint NOT NULL,
    sku_name text,
    material_name text,
    qty_kg numeric(15,3) NOT NULL,
    remarks text,
    recorded_by text,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    batch_id bigint,
    CONSTRAINT chk_additive_has_name CHECK (((sku_name IS NOT NULL) OR (material_name IS NOT NULL))),
    CONSTRAINT job_card_additive_consumption_v2_qty_kg_check CHECK ((qty_kg >= (0)::numeric))
);


--
-- Name: TABLE job_card_additive_consumption_v2; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.job_card_additive_consumption_v2 IS 'Data-keeping consumption for fully-consumed seasoning (salt, sugar, citric acid, gum powder, oils, cayenne pepper, etc.). Does NOT participate in the conservation identity — surfaced separately in the Accounting Summary so the operator can review consumption history without affecting balance closure.';


--
-- Name: COLUMN job_card_additive_consumption_v2.additive_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.job_card_additive_consumption_v2.additive_id IS '8-digit time-based BIGINT supplied by app.core.helpers.new_short_time_id; insert_with_pk_retry handles epoch-ms collisions.';


--
-- Name: job_card_balance_material; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_card_balance_material (
    balance_id integer NOT NULL,
    job_card_id integer NOT NULL,
    material_id integer,
    material_name text NOT NULL,
    balance_type text NOT NULL,
    qty_kg numeric(15,3) DEFAULT 0 NOT NULL,
    remarks text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    bom_line_id integer,
    CONSTRAINT job_card_balance_material_balance_type_check CHECK ((balance_type = ANY (ARRAY['extra_given'::text, 'returned'::text, 'wastage'::text, 'control_sample'::text])))
);


--
-- Name: job_card_balance_material_balance_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.job_card_balance_material_balance_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: job_card_balance_material_balance_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.job_card_balance_material_balance_id_seq OWNED BY public.job_card_balance_material.balance_id;


--
-- Name: job_card_balance_material_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_card_balance_material_v2 (
    balance_id bigint NOT NULL,
    job_card_id bigint NOT NULL,
    bom_line_id integer,
    material_id integer,
    material_name text NOT NULL,
    balance_type text NOT NULL,
    qty_kg numeric(15,3) DEFAULT 0 NOT NULL,
    remarks text,
    recorded_by text,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    batch_id bigint,
    CONSTRAINT job_card_balance_material_v2_balance_type_check CHECK ((balance_type = ANY (ARRAY['extra_given'::text, 'returned'::text, 'wastage'::text, 'control_sample'::text]))),
    CONSTRAINT job_card_balance_material_v2_qty_kg_check CHECK ((qty_kg >= (0)::numeric))
);


--
-- Name: job_card_phase_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_card_phase_v2 (
    phase_id bigint NOT NULL,
    job_card_id bigint NOT NULL,
    phase_number integer NOT NULL,
    phase_date date NOT NULL,
    planned_qty_kg numeric(15,3),
    produced_qty_kg numeric(15,3),
    extra_give_away_qty numeric(15,3) DEFAULT 0 NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    ended_at timestamp with time zone,
    status text DEFAULT 'open'::text NOT NULL,
    closed_by text,
    closed_at timestamp with time zone,
    notes text,
    input_qty_kg numeric(15,3),
    fg_actual_kg numeric(15,3),
    fg_actual_units numeric(15,3),
    process_loss_kg numeric(15,3),
    control_sample_kg numeric(15,3),
    is_balanced boolean,
    balance_difference_qty numeric(15,3),
    closure_remarks text,
    opened_at_ist text,
    closed_at_ist text,
    ended_at_ist text,
    CONSTRAINT chk_jcphase_closed_at_when_closed CHECK (((status <> 'closed'::text) OR (closed_at IS NOT NULL))),
    CONSTRAINT chk_jcphase_ended_at_when_terminal CHECK (((status <> ALL (ARRAY['closed'::text, 'cancelled'::text])) OR (ended_at IS NOT NULL))),
    CONSTRAINT job_card_phase_v2_phase_number_check CHECK ((phase_number > 0)),
    CONSTRAINT job_card_phase_v2_status_check CHECK ((status = ANY (ARRAY['open'::text, 'closed'::text, 'cancelled'::text])))
);


--
-- Name: COLUMN job_card_phase_v2.closed_by; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.job_card_phase_v2.closed_by IS 'User who closed the phase. Backfill rows from scripts/029_backfill_phases.py carry the sentinel "migration_029_backfill" so they can be filtered from real phase closes - used by the H2 fix in the backfill UPDATE JOINs.';


--
-- Name: job_card_batch_v2; Type: VIEW; Schema: public; Owner: -
--

CREATE VIEW public.job_card_batch_v2 AS
 SELECT phase_id AS batch_id,
    job_card_id,
    phase_number AS batch_number,
    phase_date AS batch_date,
    planned_qty_kg,
    produced_qty_kg,
    extra_give_away_qty,
    started_at,
    ended_at,
    status,
    closed_by,
    closed_at,
    notes,
    input_qty_kg,
    fg_actual_kg,
    fg_actual_units,
    process_loss_kg,
    control_sample_kg,
    is_balanced,
    balance_difference_qty,
    closure_remarks,
    opened_at_ist,
    closed_at_ist,
    ended_at_ist
   FROM public.job_card_phase_v2;


--
-- Name: VIEW job_card_batch_v2; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON VIEW public.job_card_batch_v2 IS 'Phase→Batch compat view (migration 036, extended by 045 to expose Stage-2 batch-summary columns that 038_jc_batch_per_record never wired in). Auto-updatable.';


--
-- Name: job_card_byproducts_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_card_byproducts_v2 (
    byproduct_id bigint NOT NULL,
    job_card_id bigint NOT NULL,
    category text NOT NULL,
    quantity numeric(15,3) NOT NULL,
    uom text NOT NULL,
    remarks text,
    recorded_by text,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    material_name text,
    bom_line_id integer,
    batch_id bigint,
    CONSTRAINT job_card_byproducts_v2_category_check CHECK ((category = ANY (ARRAY['tukda'::text, 'damaged'::text, 'black_stained'::text, 'without_shell'::text, 'empty_shells'::text, 'dust'::text, 'balance_material'::text, 'rejection'::text, 'control_sample'::text, 'wastage'::text, 'other'::text, 'pm_torn'::text, 'pm_damaged'::text, 'pm_misprint'::text, 'pm_rejection'::text, 'pm_wasted'::text]))),
    CONSTRAINT job_card_byproducts_v2_quantity_check CHECK ((quantity >= (0)::numeric)),
    CONSTRAINT job_card_byproducts_v2_uom_check CHECK ((uom = ANY (ARRAY['KGS'::text, 'GMS'::text, 'LTRS'::text, 'NOS'::text, 'PCS'::text, 'ROLL'::text, 'SETS'::text, 'BUNDLE'::text])))
);


--
-- Name: job_card_consumption_variance_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_card_consumption_variance_v2 (
    variance_id bigint NOT NULL,
    job_card_id bigint NOT NULL,
    material_sku_name text NOT NULL,
    bom_id integer,
    bom_version integer,
    bom_prescribed_qty numeric(15,3) NOT NULL,
    actual_consumed_qty numeric(15,3) NOT NULL,
    uom text NOT NULL,
    unit_cost_at_consumption numeric(15,4),
    variance_cost_impact numeric(15,2),
    cost_basis text,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    last_updated_at timestamp with time zone DEFAULT now() NOT NULL,
    variance_qty numeric(15,3) GENERATED ALWAYS AS ((actual_consumed_qty - bom_prescribed_qty)) STORED,
    variance_pct numeric(8,4) GENERATED ALWAYS AS (
CASE
    WHEN (bom_prescribed_qty = (0)::numeric) THEN NULL::numeric
    ELSE (((actual_consumed_qty - bom_prescribed_qty) / bom_prescribed_qty) * (100)::numeric)
END) STORED,
    CONSTRAINT job_card_consumption_variance_v2_actual_consumed_qty_check CHECK ((actual_consumed_qty >= (0)::numeric)),
    CONSTRAINT job_card_consumption_variance_v2_bom_prescribed_qty_check CHECK ((bom_prescribed_qty >= (0)::numeric)),
    CONSTRAINT job_card_consumption_variance_v2_uom_check CHECK ((uom = ANY (ARRAY['KGS'::text, 'GMS'::text, 'LTRS'::text, 'NOS'::text, 'PCS'::text, 'ROLL'::text, 'SETS'::text, 'BUNDLE'::text])))
);


--
-- Name: COLUMN job_card_consumption_variance_v2.last_updated_at; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.job_card_consumption_variance_v2.last_updated_at IS 'Touched by fn_touch_jcvar_last_updated trigger on every UPDATE. Intentionally named last_updated_at (not the v2 convention "updated_at") to make intent explicit on an upsert-heavy table - operators re-save consumption frequently and the column literally captures "last save timestamp", not the more generic "updated_at" snapshot.';


--
-- Name: job_card_environment; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_card_environment (
    env_id integer NOT NULL,
    job_card_id integer NOT NULL,
    parameter_name text NOT NULL,
    value text,
    updated_at timestamp with time zone,
    updated_by text,
    deleted_at timestamp with time zone,
    deleted_by text,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: job_card_environment_env_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.job_card_environment_env_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: job_card_environment_env_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.job_card_environment_env_id_seq OWNED BY public.job_card_environment.env_id;


--
-- Name: job_card_environment_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_card_environment_v2 (
    env_id bigint NOT NULL,
    job_card_id bigint NOT NULL,
    parameter_name text NOT NULL,
    value text,
    unit text,
    remarks text,
    recorded_by text,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone,
    updated_by text,
    deleted_at timestamp with time zone,
    deleted_by text,
    deletion_reason text
);


--
-- Name: job_card_loss_reconciliation; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_card_loss_reconciliation (
    recon_id integer NOT NULL,
    job_card_id integer NOT NULL,
    loss_category text NOT NULL,
    budgeted_loss_pct numeric(5,3),
    budgeted_loss_kg numeric(15,3),
    actual_loss_kg numeric(15,3),
    variance_kg numeric(15,3),
    remarks text,
    updated_at timestamp with time zone,
    updated_by text,
    deleted_at timestamp with time zone,
    deleted_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: job_card_loss_reconciliation_recon_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.job_card_loss_reconciliation_recon_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: job_card_loss_reconciliation_recon_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.job_card_loss_reconciliation_recon_id_seq OWNED BY public.job_card_loss_reconciliation.recon_id;


--
-- Name: job_card_loss_reconciliation_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_card_loss_reconciliation_v2 (
    recon_id bigint NOT NULL,
    job_card_id bigint NOT NULL,
    loss_category text NOT NULL,
    budgeted_loss_pct numeric(5,3),
    budgeted_loss_qty numeric(15,3),
    actual_loss_qty numeric(15,3),
    variance_qty numeric(15,3),
    uom text,
    remarks text,
    recorded_by text,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone,
    updated_by text,
    deleted_at timestamp with time zone,
    deleted_by text,
    deletion_reason text,
    CONSTRAINT job_card_loss_reconciliation_v2_loss_category_check CHECK ((loss_category = ANY (ARRAY['sorting_rejection'::text, 'roasting_loss'::text, 'packaging_rejection'::text, 'metal_detector'::text, 'spillage'::text, 'qc_sample'::text, 'other'::text]))),
    CONSTRAINT job_card_loss_reconciliation_v2_uom_check CHECK ((uom = ANY (ARRAY['KGS'::text, 'GMS'::text, 'LTRS'::text, 'NOS'::text, 'PCS'::text, 'ROLL'::text, 'SETS'::text, 'BUNDLE'::text])))
);


--
-- Name: job_card_material_consumption; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_card_material_consumption (
    consumption_id integer NOT NULL,
    job_card_id integer NOT NULL,
    rm_indent_id integer,
    material_sku_name text NOT NULL,
    item_type text DEFAULT 'rm'::text NOT NULL,
    uom text,
    bom_reqd_qty numeric(15,3) DEFAULT 0 NOT NULL,
    issued_qty numeric(15,3) DEFAULT 0,
    actual_consumed_qty numeric(15,3),
    return_qty numeric(15,3) DEFAULT 0,
    variance_qty numeric(15,3),
    remarks text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: job_card_material_consumption_consumption_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.job_card_material_consumption_consumption_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: job_card_material_consumption_consumption_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.job_card_material_consumption_consumption_id_seq OWNED BY public.job_card_material_consumption.consumption_id;


--
-- Name: job_card_material_consumption_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_card_material_consumption_v2 (
    consumption_id bigint NOT NULL,
    job_card_id bigint NOT NULL,
    material_sku_name text NOT NULL,
    input_kind text NOT NULL,
    uom text NOT NULL,
    issued_qty numeric(15,3) NOT NULL,
    actual_consumed_qty numeric(15,3) DEFAULT 0 NOT NULL,
    return_qty numeric(15,3) DEFAULT 0 NOT NULL,
    variance numeric(15,3),
    source_rm_indent_id bigint,
    source_dispatch_id bigint,
    remarks text,
    recorded_by text,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    bom_line_id integer,
    batch_id bigint,
    CONSTRAINT job_card_material_consumption_v2_actual_consumed_qty_check CHECK ((actual_consumed_qty >= (0)::numeric)),
    CONSTRAINT job_card_material_consumption_v2_input_kind_check CHECK ((input_kind = ANY (ARRAY['RM'::text, 'SFG'::text, 'WIP'::text, 'PM'::text]))),
    CONSTRAINT job_card_material_consumption_v2_issued_qty_check CHECK ((issued_qty >= (0)::numeric)),
    CONSTRAINT job_card_material_consumption_v2_return_qty_check CHECK ((return_qty >= (0)::numeric)),
    CONSTRAINT job_card_material_consumption_v2_uom_check CHECK ((uom = ANY (ARRAY['KGS'::text, 'GMS'::text, 'LTRS'::text, 'NOS'::text, 'PCS'::text, 'ROLL'::text, 'SETS'::text, 'BUNDLE'::text])))
);


--
-- Name: TABLE job_card_material_consumption_v2; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.job_card_material_consumption_v2 IS 'Actual qty consumed per input line on a v2 JC. RM rows for stage 1; SFG/WIP rows for downstream stages (one row referencing the carried qty from the prev-stage dispatch).';


--
-- Name: job_card_metal_detection; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_card_metal_detection (
    detection_id integer NOT NULL,
    job_card_id integer NOT NULL,
    check_type text NOT NULL,
    fe_pass boolean,
    nfe_pass boolean,
    ss_pass boolean,
    failed_units integer DEFAULT 0,
    remarks text,
    updated_at timestamp with time zone,
    updated_by text,
    deleted_at timestamp with time zone,
    deleted_by text,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    seal_check boolean,
    seal_failed_units integer DEFAULT 0,
    wt_check boolean,
    wt_failed_units integer DEFAULT 0,
    dough_temp_c numeric(10,2),
    oven_temp_c numeric(10,2),
    baking_temp_c numeric(10,2)
);


--
-- Name: job_card_metal_detection_detection_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.job_card_metal_detection_detection_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: job_card_metal_detection_detection_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.job_card_metal_detection_detection_id_seq OWNED BY public.job_card_metal_detection.detection_id;


--
-- Name: job_card_metal_detection_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_card_metal_detection_v2 (
    detection_id bigint NOT NULL,
    job_card_id bigint NOT NULL,
    check_type text NOT NULL,
    fe_pass boolean,
    nfe_pass boolean,
    ss_pass boolean,
    failed_units integer DEFAULT 0 NOT NULL,
    remarks text,
    recorded_by text,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone,
    updated_by text,
    deleted_at timestamp with time zone,
    deleted_by text,
    deletion_reason text,
    CONSTRAINT job_card_metal_detection_v2_check_type_check CHECK ((check_type = ANY (ARRAY['pre_packaging'::text, 'post_packaging'::text]))),
    CONSTRAINT job_card_metal_detection_v2_failed_units_check CHECK ((failed_units >= 0))
);


--
-- Name: job_card_output; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_card_output (
    output_id integer NOT NULL,
    job_card_id integer NOT NULL,
    fg_expected_units integer,
    fg_actual_units integer,
    fg_expected_kg numeric(15,3),
    fg_actual_kg numeric(15,3),
    rm_consumed_kg numeric(15,3),
    process_loss_kg numeric(15,3) DEFAULT 0,
    net_output_kg numeric(15,3) DEFAULT 0,
    yield_pct numeric(8,3) DEFAULT 0,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: job_card_output_output_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.job_card_output_output_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: job_card_output_output_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.job_card_output_output_id_seq OWNED BY public.job_card_output.output_id;


--
-- Name: job_card_output_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_card_output_v2 (
    output_id bigint NOT NULL,
    job_card_id bigint NOT NULL,
    rm_consumed_kg numeric(15,3) DEFAULT 0 NOT NULL,
    output_qty_kg numeric(15,3) NOT NULL,
    output_qty_units numeric(15,3),
    output_kind text NOT NULL,
    uom text,
    yield_pct numeric(6,3),
    notes text,
    recorded_by text,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    process_loss_kg numeric(15,3) DEFAULT 0 NOT NULL,
    phase_id bigint,
    batch_id bigint,
    CONSTRAINT job_card_output_v2_output_kind_check CHECK ((output_kind = ANY (ARRAY['SFG'::text, 'WIP'::text, 'FG'::text]))),
    CONSTRAINT job_card_output_v2_output_qty_kg_check CHECK ((output_qty_kg >= (0)::numeric))
);


--
-- Name: job_card_output_v2_output_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.job_card_output_v2_output_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: job_card_output_v2_output_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.job_card_output_v2_output_id_seq OWNED BY public.job_card_output_v2.output_id;


--
-- Name: job_card_partial_dispatch; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_card_partial_dispatch (
    dispatch_id integer NOT NULL,
    from_job_card_id integer NOT NULL,
    to_job_card_id integer NOT NULL,
    qty_kg numeric(15,3) NOT NULL,
    dispatched_at timestamp with time zone DEFAULT now() NOT NULL,
    dispatched_by text,
    CONSTRAINT job_card_partial_dispatch_qty_kg_check CHECK ((qty_kg > (0)::numeric))
);


--
-- Name: job_card_partial_dispatch_dispatch_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.job_card_partial_dispatch_dispatch_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: job_card_partial_dispatch_dispatch_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.job_card_partial_dispatch_dispatch_id_seq OWNED BY public.job_card_partial_dispatch.dispatch_id;


--
-- Name: job_card_partial_dispatch_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_card_partial_dispatch_v2 (
    dispatch_id bigint NOT NULL,
    from_job_card_id bigint NOT NULL,
    to_job_card_id bigint NOT NULL,
    qty_kg numeric(15,3) NOT NULL,
    qty_units numeric(15,3),
    dispatched_by text,
    dispatched_at timestamp with time zone DEFAULT now() NOT NULL,
    notes text,
    phase_id bigint,
    batch_id bigint,
    CONSTRAINT job_card_partial_dispatch_v2_qty_kg_check CHECK ((qty_kg > (0)::numeric))
);


--
-- Name: job_card_partial_dispatch_v2_dispatch_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.job_card_partial_dispatch_v2_dispatch_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: job_card_partial_dispatch_v2_dispatch_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.job_card_partial_dispatch_v2_dispatch_id_seq OWNED BY public.job_card_partial_dispatch_v2.dispatch_id;


--
-- Name: job_card_pm_indent; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_card_pm_indent (
    pm_indent_id integer NOT NULL,
    job_card_id integer NOT NULL,
    material_sku_name text NOT NULL,
    uom text,
    reqd_qty numeric(15,3) NOT NULL,
    loss_pct numeric(5,3) DEFAULT 0,
    gross_qty numeric(15,3) NOT NULL,
    issued_qty numeric(15,3) DEFAULT 0,
    batch_no text,
    godown text,
    scanned_box_ids text[],
    variance numeric(15,3),
    status text DEFAULT 'pending'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    store_decision text DEFAULT 'pending'::text,
    store_approved_qty numeric(15,3) DEFAULT 0,
    store_decided_by text,
    store_decided_at timestamp with time zone,
    source_location text,
    quality_grade text,
    manual_ack_by text,
    manual_ack_at timestamp with time zone,
    bom_line_id integer,
    consumed_qty numeric(15,3)
);


--
-- Name: job_card_pm_indent_pm_indent_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.job_card_pm_indent_pm_indent_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: job_card_pm_indent_pm_indent_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.job_card_pm_indent_pm_indent_id_seq OWNED BY public.job_card_pm_indent.pm_indent_id;


--
-- Name: job_card_pm_indent_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_card_pm_indent_v2 (
    pm_indent_id bigint NOT NULL,
    job_card_id bigint NOT NULL,
    material_sku_name text NOT NULL,
    uom text NOT NULL,
    reqd_qty numeric(15,3) NOT NULL,
    loss_pct numeric(5,3) DEFAULT 0,
    gross_qty numeric(15,3) NOT NULL,
    issued_qty numeric(15,3) DEFAULT 0 NOT NULL,
    batch_no text,
    godown text,
    scanned_box_ids text[],
    variance numeric(15,3),
    status text DEFAULT 'pending'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    consumed_qty numeric(15,3),
    bom_line_id integer,
    CONSTRAINT job_card_pm_indent_v2_reqd_qty_check CHECK ((reqd_qty > (0)::numeric)),
    CONSTRAINT job_card_pm_indent_v2_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'partial'::text, 'fulfilled'::text]))),
    CONSTRAINT job_card_pm_indent_v2_uom_check CHECK ((uom = ANY (ARRAY['KGS'::text, 'NOS'::text, 'ROLL'::text, 'SETS'::text, 'PCS'::text, 'BUNDLE'::text])))
);


--
-- Name: job_card_pm_indent_v2_pm_indent_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.job_card_pm_indent_v2_pm_indent_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: job_card_pm_indent_v2_pm_indent_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.job_card_pm_indent_v2_pm_indent_id_seq OWNED BY public.job_card_pm_indent_v2.pm_indent_id;


--
-- Name: job_card_qc_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_card_qc_v2 (
    qc_id bigint NOT NULL,
    job_card_id bigint NOT NULL,
    result text DEFAULT 'pending'::text NOT NULL,
    findings text,
    corrective_action text,
    inspector_user text,
    inspection_date timestamp with time zone,
    recorded_by text,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone,
    CONSTRAINT job_card_qc_v2_result_check CHECK ((result = ANY (ARRAY['pending'::text, 'pass'::text, 'fail'::text, 'conditional_pass'::text])))
);


--
-- Name: job_card_remarks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_card_remarks (
    remark_id integer NOT NULL,
    job_card_id integer NOT NULL,
    remark_type text NOT NULL,
    content text,
    recorded_by text,
    updated_at timestamp with time zone,
    updated_by text,
    deleted_at timestamp with time zone,
    deleted_by text,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: job_card_remarks_remark_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.job_card_remarks_remark_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: job_card_remarks_remark_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.job_card_remarks_remark_id_seq OWNED BY public.job_card_remarks.remark_id;


--
-- Name: job_card_remarks_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_card_remarks_v2 (
    remark_id bigint NOT NULL,
    job_card_id bigint NOT NULL,
    remark_type text NOT NULL,
    content text NOT NULL,
    recorded_by text,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone,
    updated_by text,
    deleted_at timestamp with time zone,
    deleted_by text,
    deletion_reason text,
    CONSTRAINT job_card_remarks_v2_remark_type_check CHECK ((remark_type = ANY (ARRAY['observation'::text, 'deviation'::text, 'corrective_action'::text])))
);


--
-- Name: job_card_rm_indent; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_card_rm_indent (
    rm_indent_id integer NOT NULL,
    job_card_id integer NOT NULL,
    material_sku_name text NOT NULL,
    uom text,
    reqd_qty numeric(15,3) NOT NULL,
    loss_pct numeric(5,3) DEFAULT 0,
    gross_qty numeric(15,3) NOT NULL,
    issued_qty numeric(15,3) DEFAULT 0,
    batch_no text,
    godown text,
    scanned_box_ids text[],
    variance numeric(15,3),
    status text DEFAULT 'pending'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    store_decision text DEFAULT 'pending'::text,
    store_approved_qty numeric(15,3) DEFAULT 0,
    store_decided_by text,
    store_decided_at timestamp with time zone,
    source_location text,
    quality_grade text,
    manual_ack_by text,
    manual_ack_at timestamp with time zone,
    bom_line_id integer,
    consumed_qty numeric(15,3)
);


--
-- Name: job_card_rm_indent_rm_indent_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.job_card_rm_indent_rm_indent_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: job_card_rm_indent_rm_indent_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.job_card_rm_indent_rm_indent_id_seq OWNED BY public.job_card_rm_indent.rm_indent_id;


--
-- Name: job_card_rm_indent_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_card_rm_indent_v2 (
    rm_indent_id bigint NOT NULL,
    job_card_id bigint NOT NULL,
    material_sku_name text NOT NULL,
    uom text NOT NULL,
    reqd_qty numeric(15,3) NOT NULL,
    loss_pct numeric(5,3) DEFAULT 0,
    gross_qty numeric(15,3) NOT NULL,
    issued_qty numeric(15,3) DEFAULT 0 NOT NULL,
    batch_no text,
    godown text,
    scanned_box_ids text[],
    variance numeric(15,3),
    status text DEFAULT 'pending'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    consumed_qty numeric(15,3),
    bom_line_id integer,
    CONSTRAINT job_card_rm_indent_v2_reqd_qty_check CHECK ((reqd_qty > (0)::numeric)),
    CONSTRAINT job_card_rm_indent_v2_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'partial'::text, 'fulfilled'::text]))),
    CONSTRAINT job_card_rm_indent_v2_uom_check CHECK ((uom = ANY (ARRAY['KGS'::text, 'GMS'::text, 'LTRS'::text, 'NOS'::text])))
);


--
-- Name: job_card_rm_indent_v2_rm_indent_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.job_card_rm_indent_v2_rm_indent_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: job_card_rm_indent_v2_rm_indent_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.job_card_rm_indent_v2_rm_indent_id_seq OWNED BY public.job_card_rm_indent_v2.rm_indent_id;


--
-- Name: job_card_shift_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_card_shift_log (
    log_id bigint NOT NULL,
    job_card_id integer NOT NULL,
    shift text NOT NULL,
    shift_date date NOT NULL,
    start_at timestamp with time zone NOT NULL,
    end_at timestamp with time zone,
    paused_minutes integer DEFAULT 0 NOT NULL,
    operator_name text,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT job_card_shift_log_check CHECK (((end_at IS NULL) OR (end_at >= start_at))),
    CONSTRAINT job_card_shift_log_paused_minutes_check CHECK ((paused_minutes >= 0)),
    CONSTRAINT job_card_shift_log_shift_check CHECK ((shift = ANY (ARRAY['A'::text, 'B'::text, 'C'::text, 'general'::text])))
);


--
-- Name: TABLE job_card_shift_log; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.job_card_shift_log IS 'Append-only time segments per job card. Multiple rows = multi-shift / multi-day stage. An open segment is one where end_at IS NULL.';


--
-- Name: job_card_shift_log_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.job_card_shift_log_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: job_card_shift_log_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.job_card_shift_log_log_id_seq OWNED BY public.job_card_shift_log.log_id;


--
-- Name: job_card_shift_log_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_card_shift_log_v2 (
    log_id bigint NOT NULL,
    job_card_id bigint NOT NULL,
    shift text NOT NULL,
    shift_date date NOT NULL,
    start_at timestamp with time zone NOT NULL,
    end_at timestamp with time zone,
    paused_minutes integer DEFAULT 0 NOT NULL,
    operator_name text,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    phase_id bigint,
    batch_id bigint,
    CONSTRAINT job_card_shift_log_v2_check CHECK (((end_at IS NULL) OR (end_at >= start_at))),
    CONSTRAINT job_card_shift_log_v2_paused_minutes_check CHECK ((paused_minutes >= 0)),
    CONSTRAINT job_card_shift_log_v2_shift_check CHECK ((shift = ANY (ARRAY['A'::text, 'B'::text, 'C'::text, 'general'::text])))
);


--
-- Name: job_card_shift_log_v2_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.job_card_shift_log_v2_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: job_card_shift_log_v2_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.job_card_shift_log_v2_log_id_seq OWNED BY public.job_card_shift_log_v2.log_id;


--
-- Name: job_card_sign_off; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_card_sign_off (
    sign_off_id integer NOT NULL,
    job_card_id integer NOT NULL,
    sign_off_type text NOT NULL,
    name text,
    signed_at timestamp with time zone
);


--
-- Name: job_card_sign_off_sign_off_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.job_card_sign_off_sign_off_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: job_card_sign_off_sign_off_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.job_card_sign_off_sign_off_id_seq OWNED BY public.job_card_sign_off.sign_off_id;


--
-- Name: job_card_sign_off_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_card_sign_off_v2 (
    sign_off_id bigint NOT NULL,
    job_card_id bigint NOT NULL,
    role text NOT NULL,
    signed_by text NOT NULL,
    signed_at timestamp with time zone DEFAULT now() NOT NULL,
    notes text
);


--
-- Name: job_card_sign_off_v2_sign_off_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.job_card_sign_off_v2_sign_off_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: job_card_sign_off_v2_sign_off_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.job_card_sign_off_v2_sign_off_id_seq OWNED BY public.job_card_sign_off_v2.sign_off_id;


--
-- Name: job_card_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_card_v2 (
    job_card_id bigint NOT NULL,
    job_card_number text NOT NULL,
    plan_id bigint NOT NULL,
    plan_line_id bigint NOT NULL,
    plan_step_id bigint NOT NULL,
    bom_id integer,
    step_number integer NOT NULL,
    process_name text NOT NULL,
    stage text NOT NULL,
    fg_sku_name text NOT NULL,
    customer_name text,
    batch_number text NOT NULL,
    planned_qty_kg numeric(15,3) NOT NULL,
    planned_qty_units numeric(15,3),
    uom text,
    input_kind text NOT NULL,
    output_kind text NOT NULL,
    factory text NOT NULL,
    floor text,
    entity text NOT NULL,
    machine_id integer,
    assigned_to_team_leader text,
    team_members text[],
    is_locked boolean DEFAULT true NOT NULL,
    locked_reason text,
    force_unlocked boolean DEFAULT false,
    force_unlock_by text,
    force_unlock_reason text,
    force_unlock_at timestamp with time zone,
    status text DEFAULT 'locked'::text NOT NULL,
    start_time timestamp with time zone,
    end_time timestamp with time zone,
    total_time_min numeric(10,2) DEFAULT 0 NOT NULL,
    prev_job_card_id bigint,
    next_job_card_id bigint,
    carried_qty_kg numeric(15,3) DEFAULT 0 NOT NULL,
    dispatched_to_next_kg numeric(15,3) DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone,
    updated_by text,
    deleted_at timestamp with time zone,
    deleted_by text,
    cancellation_reason text,
    force_closed boolean DEFAULT false NOT NULL,
    force_close_request_id bigint,
    force_close_by text,
    force_close_at timestamp with time zone,
    cancelled_snapshot jsonb,
    input_code text,
    output_code text,
    CONSTRAINT job_card_v2_entity_check CHECK ((entity = ANY (ARRAY['cfpl'::text, 'cdpl'::text]))),
    CONSTRAINT job_card_v2_factory_check CHECK ((factory = ANY (ARRAY['W-202'::text, 'A-185'::text]))),
    CONSTRAINT job_card_v2_input_kind_check CHECK ((input_kind = ANY (ARRAY['RM'::text, 'SFG'::text, 'WIP'::text]))),
    CONSTRAINT job_card_v2_output_kind_check CHECK ((output_kind = ANY (ARRAY['SFG'::text, 'WIP'::text, 'FG'::text]))),
    CONSTRAINT job_card_v2_planned_qty_kg_check CHECK ((planned_qty_kg > (0)::numeric)),
    CONSTRAINT job_card_v2_status_check CHECK ((status = ANY (ARRAY['locked'::text, 'unlocked'::text, 'assigned'::text, 'material_received'::text, 'in_progress'::text, 'completed'::text, 'closed'::text, 'cancelled'::text])))
);


--
-- Name: TABLE job_card_v2; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.job_card_v2 IS 'v2 job card. One row per (plan line × plan step). PK is application-supplied 8-digit time-based BIGINT. plan_id / plan_line_id / plan_step_id are NOT NULL because v2 JCs are always derived from an approved plan.';


--
-- Name: COLUMN job_card_v2.input_kind; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.job_card_v2.input_kind IS 'Material kind received at this stage. First stage in the chain = RM.';


--
-- Name: COLUMN job_card_v2.output_kind; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.job_card_v2.output_kind IS 'Material kind produced by this stage. Last stage in the chain = FG; intermediate stages produce WIP that becomes the next stage''s SFG input.';


--
-- Name: COLUMN job_card_v2.force_closed; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.job_card_v2.force_closed IS 'TRUE when /complete was honored with an R8 unbalanced-close override. force_close_request_id, force_close_by, force_close_at carry the audit.';


--
-- Name: COLUMN job_card_v2.cancelled_snapshot; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.job_card_v2.cancelled_snapshot IS 'Full JC + linked-tables snapshot taken at cancel time. Set by cancel_job_card service in the same txn that flips status to ''cancelled''. NULL on JCs that were never cancelled or were cancelled before migration 043.';


--
-- Name: job_card_weight_check; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_card_weight_check (
    check_id integer NOT NULL,
    job_card_id integer NOT NULL,
    sample_number integer NOT NULL,
    net_weight numeric(15,3),
    gross_weight numeric(15,3),
    leak_test_pass boolean,
    updated_at timestamp with time zone,
    updated_by text,
    deleted_at timestamp with time zone,
    deleted_by text,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    target_wt_g numeric(10,2),
    tolerance_g numeric(10,2),
    accept_range_min numeric(10,2),
    accept_range_max numeric(10,2)
);


--
-- Name: job_card_weight_check_check_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.job_card_weight_check_check_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: job_card_weight_check_check_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.job_card_weight_check_check_id_seq OWNED BY public.job_card_weight_check.check_id;


--
-- Name: job_card_weight_check_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.job_card_weight_check_v2 (
    check_id bigint NOT NULL,
    job_card_id bigint NOT NULL,
    sample_number integer NOT NULL,
    net_weight numeric(15,3),
    gross_weight numeric(15,3),
    leak_test_pass boolean,
    remarks text,
    recorded_by text,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone,
    updated_by text,
    deleted_at timestamp with time zone,
    deleted_by text,
    deletion_reason text
);


--
-- Name: legacy_import_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.legacy_import_log (
    import_id integer NOT NULL,
    batch_id text NOT NULL,
    item_code text NOT NULL,
    generated_at timestamp with time zone DEFAULT now() NOT NULL,
    imported_by text,
    source_file_ref text,
    entity text,
    CONSTRAINT legacy_import_log_entity_check CHECK ((entity = ANY (ARRAY['cfpl'::text, 'cdpl'::text])))
);


--
-- Name: legacy_import_log_import_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.legacy_import_log_import_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: legacy_import_log_import_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.legacy_import_log_import_id_seq OWNED BY public.legacy_import_log.import_id;


--
-- Name: log_edit; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.log_edit (
    log_id integer NOT NULL,
    table_name text NOT NULL,
    record_id integer NOT NULL,
    field_name text,
    action text NOT NULL,
    old_value text,
    new_value text,
    changed_by integer,
    changed_at timestamp with time zone DEFAULT now() NOT NULL,
    request_id text,
    module text DEFAULT 'so_intake'::text NOT NULL
);


--
-- Name: log_edit_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.log_edit_log_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: log_edit_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.log_edit_log_id_seq OWNED BY public.log_edit.log_id;


--
-- Name: lot_block; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.lot_block (
    id integer NOT NULL,
    block_id text NOT NULL,
    transaction_no text,
    lot_number text NOT NULL,
    batch_id text,
    blocked_for_so text,
    blocked_for_customer text,
    blocked_by_user text NOT NULL,
    blocked_at timestamp with time zone DEFAULT now() NOT NULL,
    skip_reason text,
    comment text,
    previous_so text,
    force_assigned_by text,
    force_assigned_at timestamp with time zone,
    override_comment text,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: lot_block_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.lot_block_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: lot_block_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.lot_block_id_seq OWNED BY public.lot_block.id;


--
-- Name: machine; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.machine (
    machine_id integer NOT NULL,
    machine_name text NOT NULL,
    machine_type text,
    category text,
    capable_stages text[],
    floor text,
    factory text,
    status text DEFAULT 'active'::text NOT NULL,
    entity text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    allocation text DEFAULT 'idle'::text NOT NULL,
    CONSTRAINT machine_entity_check CHECK ((entity = ANY (ARRAY['cfpl'::text, 'cdpl'::text])))
);


--
-- Name: machine_capacity; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.machine_capacity (
    capacity_id integer NOT NULL,
    machine_id integer NOT NULL,
    stage text NOT NULL,
    item_group text NOT NULL,
    capacity_kg_per_hr numeric(15,3) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: machine_capacity_capacity_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.machine_capacity_capacity_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: machine_capacity_capacity_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.machine_capacity_capacity_id_seq OWNED BY public.machine_capacity.capacity_id;


--
-- Name: machine_machine_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.machine_machine_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: machine_machine_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.machine_machine_id_seq OWNED BY public.machine.machine_id;


--
-- Name: material_document; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.material_document (
    id integer NOT NULL,
    mat_doc_id text NOT NULL,
    doc_date date DEFAULT CURRENT_DATE NOT NULL,
    posting_date date DEFAULT CURRENT_DATE NOT NULL,
    movement_type text NOT NULL,
    reference_type text,
    reference_id text,
    created_by text NOT NULL,
    entity text DEFAULT 'cfpl'::text,
    reversal_of text,
    is_reversal boolean DEFAULT false,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    sample_requisition_id integer,
    gate_pass_id integer,
    converted_to_external boolean DEFAULT false NOT NULL,
    CONSTRAINT material_document_entity_check CHECK ((entity = ANY (ARRAY['cfpl'::text, 'cdpl'::text])))
);


--
-- Name: material_document_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.material_document_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: material_document_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.material_document_id_seq OWNED BY public.material_document.id;


--
-- Name: material_document_line; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.material_document_line (
    id integer NOT NULL,
    mat_doc_id text NOT NULL,
    line_number integer DEFAULT 1 NOT NULL,
    sku_name text NOT NULL,
    batch_id text,
    movement_type text NOT NULL,
    quantity_kg numeric(15,3) NOT NULL,
    uom text DEFAULT 'kg'::text,
    from_location text,
    to_location text,
    from_status text,
    to_status text,
    lot_number text,
    box_id text
);


--
-- Name: material_document_line_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.material_document_line_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: material_document_line_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.material_document_line_id_seq OWNED BY public.material_document_line.id;


--
-- Name: movement_type_ref; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.movement_type_ref (
    movement_type text NOT NULL,
    description text NOT NULL,
    direction text NOT NULL,
    affects_stock boolean DEFAULT true,
    reversal_type text,
    CONSTRAINT movement_type_ref_direction_check CHECK ((direction = ANY (ARRAY['IN'::text, 'OUT'::text, 'TRANSFER'::text, 'REVERSAL'::text])))
);


--
-- Name: ncr_event_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ncr_event_log (
    event_id bigint NOT NULL,
    event_type text NOT NULL,
    entity_type text DEFAULT 'ncr'::text NOT NULL,
    entity_id text NOT NULL,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL,
    occurred_by text,
    payload jsonb
);


--
-- Name: ncr_event_log_event_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ncr_event_log_event_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ncr_event_log_event_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ncr_event_log_event_id_seq OWNED BY public.ncr_event_log.event_id;


--
-- Name: ncr_parameter_detail; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ncr_parameter_detail (
    detail_id bigint NOT NULL,
    ncr_no text NOT NULL,
    parameter_id integer,
    parameter_name text,
    observed_value_text text,
    observed_value_num numeric,
    spec_min numeric,
    spec_max numeric,
    deviation_pct numeric,
    severity text,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ncr_param_severity_check CHECK (((severity IS NULL) OR (severity = ANY (ARRAY['minor'::text, 'major'::text, 'critical'::text]))))
);


--
-- Name: ncr_parameter_detail_detail_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ncr_parameter_detail_detail_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ncr_parameter_detail_detail_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ncr_parameter_detail_detail_id_seq OWNED BY public.ncr_parameter_detail.detail_id;


--
-- Name: ncr_record; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ncr_record (
    ncr_no text NOT NULL,
    status text DEFAULT 'raised'::text NOT NULL,
    severity text,
    raised_via text,
    inspection_id text,
    transaction_no text,
    line_number integer,
    sku_id integer,
    sku_name text,
    supplier_id integer NOT NULL,
    supplier_name text,
    lot_number text,
    rejected_qty numeric NOT NULL,
    summary text,
    documented_date date,
    disposition text,
    disposition_qty numeric,
    financial_impact numeric,
    rationale text,
    rtv_vehicle_no text,
    rtv_lr_no text,
    concurrence_user_id text,
    disposition_at timestamp with time zone,
    disposition_by text,
    raised_at timestamp with time zone DEFAULT now() NOT NULL,
    raised_by text,
    requires_dual_approval boolean DEFAULT false NOT NULL,
    dual_approval_by text,
    dual_approval_at timestamp with time zone,
    cancel_reason text,
    cancelled_at timestamp with time zone,
    cancelled_by text,
    reopen_reason text,
    reopened_at timestamp with time zone,
    reopened_by text,
    closed_at timestamp with time zone,
    closure_tat_days numeric,
    entity text,
    CONSTRAINT ncr_record_disposition_check CHECK (((disposition IS NULL) OR (disposition = ANY (ARRAY['accept_with_concession'::text, 'reject'::text, 'rework'::text, 'return_to_vendor'::text, 'scrap'::text])))),
    CONSTRAINT ncr_record_qty_positive CHECK ((rejected_qty > (0)::numeric)),
    CONSTRAINT ncr_record_severity_check CHECK (((severity IS NULL) OR (severity = ANY (ARRAY['minor'::text, 'major'::text, 'critical'::text])))),
    CONSTRAINT ncr_record_status_check CHECK ((status = ANY (ARRAY['raised'::text, 'dispositioned'::text, 'capa_pending'::text, 'capa_submitted'::text, 'capa_failed_verification'::text, 'closed'::text, 'cancelled'::text])))
);


--
-- Name: ncr_record_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ncr_record_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ncr_supplier_action; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ncr_supplier_action (
    action_id bigint NOT NULL,
    ncr_no text NOT NULL,
    round_no integer NOT NULL,
    root_cause text NOT NULL,
    corrective_action text NOT NULL,
    preventive_action text,
    target_date date NOT NULL,
    responsible_person text NOT NULL,
    evidence_s3_keys text[] DEFAULT '{}'::text[] NOT NULL,
    submitted_at timestamp with time zone DEFAULT now() NOT NULL,
    submitted_by text,
    submitted_via text DEFAULT 'purchase_relay'::text NOT NULL,
    verified_at timestamp with time zone,
    verified_by text,
    is_effective boolean,
    verification_notes text,
    verification_evidence_s3_keys text[] DEFAULT '{}'::text[] NOT NULL,
    CONSTRAINT ncr_capa_via_check CHECK ((submitted_via = ANY (ARRAY['vendor_email'::text, 'vendor_portal'::text, 'purchase_relay'::text])))
);


--
-- Name: ncr_supplier_action_action_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.ncr_supplier_action_action_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ncr_supplier_action_action_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.ncr_supplier_action_action_id_seq OWNED BY public.ncr_supplier_action.action_id;


--
-- Name: npd_authorized_users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.npd_authorized_users (
    id integer NOT NULL,
    user_id integer NOT NULL,
    capability text NOT NULL,
    active boolean DEFAULT true NOT NULL,
    granted_by integer,
    granted_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT npd_authorized_users_capability_check CHECK ((capability = ANY (ARRAY['AUTHOR'::text, 'CLOSE'::text, 'PROMOTE'::text])))
);


--
-- Name: npd_authorized_users_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.npd_authorized_users_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: npd_authorized_users_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.npd_authorized_users_id_seq OWNED BY public.npd_authorized_users.id;


--
-- Name: npd_dev_job_card_lines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.npd_dev_job_card_lines (
    id integer NOT NULL,
    dev_jc_id bigint NOT NULL,
    sku_id integer,
    sku_name text NOT NULL,
    qty numeric(15,3) NOT NULL,
    uom text NOT NULL,
    item_type text,
    line_order integer DEFAULT 0 NOT NULL,
    notes text,
    ownership text DEFAULT 'OWN'::text NOT NULL,
    is_off_master boolean DEFAULT false NOT NULL,
    customer_lot_ref text,
    received_qty numeric(15,3),
    phase_id bigint,
    CONSTRAINT npd_dev_job_card_lines_item_type_check CHECK ((item_type = ANY (ARRAY['rm'::text, 'pm'::text]))),
    CONSTRAINT npd_dev_job_card_lines_ownership_check CHECK ((ownership = ANY (ARRAY['OWN'::text, 'CUSTOMER'::text]))),
    CONSTRAINT npd_dev_job_card_lines_qty_check CHECK ((qty >= (0)::numeric))
);


--
-- Name: npd_dev_job_card_lines_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.npd_dev_job_card_lines_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: npd_dev_job_card_lines_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.npd_dev_job_card_lines_id_seq OWNED BY public.npd_dev_job_card_lines.id;


--
-- Name: npd_dev_job_card_phases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.npd_dev_job_card_phases (
    phase_id bigint NOT NULL,
    dev_jc_id bigint NOT NULL,
    phase_number integer NOT NULL,
    name text NOT NULL,
    status text DEFAULT 'PENDING'::text NOT NULL,
    started_at timestamp with time zone,
    started_by integer,
    completed_at timestamp with time zone,
    completed_by integer,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    output_qty numeric(15,3),
    output_uom text,
    rm_consumed_qty numeric(15,3),
    wastage_qty numeric(15,3),
    extra_give_away_qty numeric(15,3),
    yield_pct numeric(8,3),
    CONSTRAINT npd_dev_job_card_phases_status_check CHECK ((status = ANY (ARRAY['PENDING'::text, 'IN_PROGRESS'::text, 'COMPLETED'::text])))
);


--
-- Name: npd_dev_job_cards; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.npd_dev_job_cards (
    id bigint NOT NULL,
    title text NOT NULL,
    description text,
    warehouse text,
    base_bom_id integer,
    fg_sku_id integer,
    fg_sku_name text,
    target_qty numeric(15,3),
    uom text,
    status text DEFAULT 'DRAFT'::text NOT NULL,
    output_qty numeric(15,3),
    output_uom text,
    yield_pct numeric(7,3),
    output_notes text,
    promoted_bom_id integer,
    cancellation_reason text,
    created_by integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    started_by integer,
    started_at timestamp with time zone,
    closed_by integer,
    closed_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    fg_sample_batch_id text,
    dispatched_at timestamp with time zone,
    dispatched_by integer,
    dispatch_recipient text,
    dispatch_qty numeric(15,3),
    dispatch_mat_doc_id text,
    rm_consumed_qty numeric(15,3),
    wastage_qty numeric(15,3),
    extra_give_away_qty numeric(15,3),
    source_requisition_id bigint,
    company_name text,
    customer_name text,
    customer_contact text,
    customer_ship_to_address text,
    mode_of_transport text,
    expected_dispatch_date date,
    confirmed_dispatch_date date,
    pcs numeric(15,3),
    weight_per_piece numeric(15,4),
    returnable boolean,
    non_returnable boolean,
    paid boolean,
    amount numeric(12,2),
    CONSTRAINT npd_dev_job_cards_status_check CHECK ((status = ANY (ARRAY['DRAFT'::text, 'IN_DEVELOPMENT'::text, 'CLOSED'::text, 'CANCELLED'::text])))
);


--
-- Name: npd_dev_job_cards_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.npd_dev_job_cards_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: npd_dev_job_cards_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.npd_dev_job_cards_id_seq OWNED BY public.npd_dev_job_cards.id;


--
-- Name: npd_dev_promote_approval; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.npd_dev_promote_approval (
    id bigint NOT NULL,
    promote_request_id bigint NOT NULL,
    approver_kind text NOT NULL,
    approver_user_id integer,
    status text DEFAULT 'PENDING'::text NOT NULL,
    remarks text,
    decided_at timestamp with time zone,
    CONSTRAINT npd_dev_promote_approval_approver_kind_check CHECK ((approver_kind = ANY (ARRAY['INV_MGR'::text, 'REQUESTOR_BH'::text]))),
    CONSTRAINT npd_dev_promote_approval_status_check CHECK ((status = ANY (ARRAY['PENDING'::text, 'ACCEPTED'::text, 'REJECTED'::text])))
);


--
-- Name: npd_dev_promote_request; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.npd_dev_promote_request (
    id bigint NOT NULL,
    dev_jc_id bigint NOT NULL,
    promote_phase_id bigint,
    close_payload jsonb NOT NULL,
    status text DEFAULT 'PENDING'::text NOT NULL,
    created_by integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    decided_at timestamp with time zone,
    CONSTRAINT npd_dev_promote_request_status_check CHECK ((status = ANY (ARRAY['PENDING'::text, 'APPROVED'::text, 'REJECTED'::text, 'VOID'::text])))
);


--
-- Name: npd_draft_bom_lines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.npd_draft_bom_lines (
    id integer NOT NULL,
    draft_bom_id integer NOT NULL,
    sku_id integer,
    sku_name text NOT NULL,
    qty numeric NOT NULL,
    uom text NOT NULL,
    item_type text,
    delta_type text DEFAULT 'UNCHANGED'::text NOT NULL,
    original_qty numeric,
    line_order integer DEFAULT 0 NOT NULL,
    notes text,
    ownership text DEFAULT 'OWN'::text NOT NULL,
    is_off_master boolean DEFAULT false NOT NULL,
    customer_lot_ref text,
    received_qty numeric(15,3),
    CONSTRAINT npd_draft_bom_lines_delta_type_check CHECK ((delta_type = ANY (ARRAY['UNCHANGED'::text, 'ADDED'::text, 'MODIFIED'::text, 'REMOVED'::text]))),
    CONSTRAINT npd_draft_bom_lines_item_type_check CHECK ((item_type = ANY (ARRAY['rm'::text, 'pm'::text]))),
    CONSTRAINT npd_draft_bom_lines_ownership_check CHECK ((ownership = ANY (ARRAY['OWN'::text, 'CUSTOMER'::text]))),
    CONSTRAINT npd_draft_bom_lines_qty_check CHECK ((qty >= (0)::numeric))
);


--
-- Name: npd_draft_bom_lines_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.npd_draft_bom_lines_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: npd_draft_bom_lines_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.npd_draft_bom_lines_id_seq OWNED BY public.npd_draft_bom_lines.id;


--
-- Name: npd_draft_boms; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.npd_draft_boms (
    id integer NOT NULL,
    requisition_id integer NOT NULL,
    base_bom_id integer,
    fg_sku_id integer,
    fg_sku_name text,
    description text,
    status text DEFAULT 'DRAFT'::text NOT NULL,
    promoted_bom_id integer,
    promoted_at timestamp with time zone,
    promoted_by integer,
    promotion_approval_id integer,
    created_by integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT npd_draft_boms_status_check CHECK ((status = ANY (ARRAY['DRAFT'::text, 'USED'::text, 'PROMOTED'::text, 'ARCHIVED'::text])))
);


--
-- Name: npd_draft_boms_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.npd_draft_boms_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: npd_draft_boms_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.npd_draft_boms_id_seq OWNED BY public.npd_draft_boms.id;


--
-- Name: off_grade_inventory; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.off_grade_inventory (
    id integer NOT NULL,
    offgrade_id text NOT NULL,
    original_tr_number text,
    original_lot_number text,
    item_description text NOT NULL,
    material_type text,
    qty numeric(12,2),
    net_weight numeric(12,3),
    source_type text,
    source_id text,
    condition_notes text,
    disposition text DEFAULT 'Pending Decision'::text NOT NULL,
    management_decision_by text,
    management_decision_at timestamp with time zone,
    entity text DEFAULT 'cfpl'::text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT off_grade_inventory_disposition_check CHECK ((disposition = ANY (ARRAY['Sell'::text, 'Discard'::text, 'Pending Decision'::text]))),
    CONSTRAINT off_grade_inventory_entity_check CHECK ((entity = ANY (ARRAY['cfpl'::text, 'cdpl'::text]))),
    CONSTRAINT off_grade_inventory_source_type_check CHECK ((source_type = ANY (ARRAY['RTV'::text, 'JC_Closure_Rejection'::text, 'QC_Rejection'::text])))
);


--
-- Name: off_grade_inventory_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.off_grade_inventory_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: off_grade_inventory_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.off_grade_inventory_id_seq OWNED BY public.off_grade_inventory.id;


--
-- Name: offgrade_consumption; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.offgrade_consumption (
    consumption_id integer NOT NULL,
    offgrade_id integer NOT NULL,
    job_card_id integer NOT NULL,
    qty_used_kg numeric(15,3) NOT NULL,
    consumed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: offgrade_consumption_consumption_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.offgrade_consumption_consumption_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: offgrade_consumption_consumption_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.offgrade_consumption_consumption_id_seq OWNED BY public.offgrade_consumption.consumption_id;


--
-- Name: offgrade_inventory; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.offgrade_inventory (
    offgrade_id integer NOT NULL,
    source_product text NOT NULL,
    item_group text,
    category text,
    grade text,
    available_qty_kg numeric(15,3) DEFAULT 0 NOT NULL,
    production_date date,
    expiry_date date,
    job_card_id integer,
    status text DEFAULT 'available'::text NOT NULL,
    entity text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT offgrade_inventory_entity_check CHECK ((entity = ANY (ARRAY['cfpl'::text, 'cdpl'::text])))
);


--
-- Name: offgrade_inventory_offgrade_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.offgrade_inventory_offgrade_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: offgrade_inventory_offgrade_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.offgrade_inventory_offgrade_id_seq OWNED BY public.offgrade_inventory.offgrade_id;


--
-- Name: offgrade_reuse_rule; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.offgrade_reuse_rule (
    rule_id integer NOT NULL,
    source_item_group text NOT NULL,
    target_item_group text NOT NULL,
    max_substitution_pct numeric(5,3) NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: offgrade_reuse_rule_rule_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.offgrade_reuse_rule_rule_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: offgrade_reuse_rule_rule_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.offgrade_reuse_rule_rule_id_seq OWNED BY public.offgrade_reuse_rule.rule_id;


--
-- Name: pending_transfer_stock; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pending_transfer_stock (
    id bigint NOT NULL,
    transfer_out_id bigint,
    box_id text,
    status text,
    destination_table text
);


--
-- Name: po_box; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.po_box (
    box_id text NOT NULL,
    transaction_no text NOT NULL,
    line_number integer NOT NULL,
    section_number integer NOT NULL,
    box_number integer NOT NULL,
    net_weight numeric(15,3),
    gross_weight numeric(15,3),
    lot_number text,
    count integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: po_event_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.po_event_log (
    event_id bigint NOT NULL,
    transaction_no text NOT NULL,
    entity text NOT NULL,
    event_type text NOT NULL,
    actor_user_id text,
    actor_role text,
    reason text,
    payload jsonb,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: po_event_log_event_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.po_event_log_event_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: po_event_log_event_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.po_event_log_event_id_seq OWNED BY public.po_event_log.event_id;


--
-- Name: po_header; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.po_header (
    transaction_no text NOT NULL,
    entity text NOT NULL,
    po_date date,
    voucher_type text,
    po_number text,
    order_reference_no text,
    narration text,
    vendor_supplier_name text,
    gross_total numeric(15,3),
    total_amount numeric(15,3),
    sgst_amount numeric(15,3),
    cgst_amount numeric(15,3),
    igst_amount numeric(15,3),
    round_off numeric(15,3),
    freight_transport_local numeric(15,3),
    apmc_tax numeric(15,3),
    packing_charges numeric(15,3),
    freight_transport_charges numeric(15,3),
    loading_unloading_charges numeric(15,3),
    other_charges_non_gst numeric(15,3),
    customer_party_name text,
    vehicle_number text,
    transporter_name text,
    lr_number text,
    source_location text,
    destination_location text,
    challan_number text,
    invoice_number text,
    grn_number text,
    system_grn_date timestamp with time zone,
    purchased_by text,
    inward_authority text,
    warehouse text,
    status text DEFAULT 'pending'::text NOT NULL,
    approved_by text,
    approved_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    is_deleted boolean DEFAULT false,
    deleted_at timestamp with time zone,
    deleted_by text,
    delete_reason text,
    CONSTRAINT po_header_entity_check CHECK ((entity = ANY (ARRAY['cfpl'::text, 'cdpl'::text])))
);


--
-- Name: po_line; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.po_line (
    transaction_no text NOT NULL,
    line_number integer NOT NULL,
    sku_name text,
    uom text,
    pack_count integer,
    po_weight numeric(15,3),
    rate numeric(15,3),
    amount numeric(15,3),
    particulars text,
    item_category text,
    sub_category text,
    item_type text,
    sales_group text,
    gst_rate numeric(15,3),
    match_score numeric(5,3),
    match_source text,
    carton_weight numeric(15,3),
    status text DEFAULT 'pending'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    gr_tolerance_pct numeric(5,2) DEFAULT 5.0,
    received_qty_kg numeric(15,3) DEFAULT 0
);


--
-- Name: po_section; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.po_section (
    transaction_no text NOT NULL,
    line_number integer NOT NULL,
    section_number integer NOT NULL,
    lot_number text,
    box_count integer,
    manufacturing_date text,
    expiry_date text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: process_loss; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.process_loss (
    loss_id integer NOT NULL,
    job_card_id integer,
    product_name text NOT NULL,
    item_group text,
    machine_name text,
    stage text,
    loss_kg numeric(15,3) NOT NULL,
    loss_pct numeric(5,3),
    loss_category text,
    batch_number text,
    production_date date,
    entity text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT process_loss_entity_check CHECK ((entity = ANY (ARRAY['cfpl'::text, 'cdpl'::text])))
);


--
-- Name: process_loss_loss_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.process_loss_loss_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: process_loss_loss_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.process_loss_loss_id_seq OWNED BY public.process_loss.loss_id;


--
-- Name: production_indent; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.production_indent (
    id integer NOT NULL,
    prod_indent_id text NOT NULL,
    item_description text NOT NULL,
    item_category text,
    sub_category text,
    material_type text NOT NULL,
    uom text DEFAULT 'kg'::text,
    required_qty numeric(12,2) NOT NULL,
    available_qty numeric(12,2) DEFAULT 0,
    shortfall_qty numeric(12,2) DEFAULT 0,
    triggered_by_job_card text,
    triggered_by_so text,
    customer_name text,
    maker_user text NOT NULL,
    checker_user text,
    checker_comment text,
    status text DEFAULT 'draft'::text NOT NULL,
    linked_internal_order text,
    linked_internal_jc text,
    entity text DEFAULT 'cfpl'::text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    approved_at timestamp with time zone,
    fulfilled_at timestamp with time zone,
    cancel_reason text,
    indent_value numeric(15,2),
    approval_level text DEFAULT 'standard'::text,
    CONSTRAINT production_indent_approval_level_check CHECK ((approval_level = ANY (ARRAY['auto'::text, 'standard'::text, 'management'::text]))),
    CONSTRAINT production_indent_entity_check CHECK ((entity = ANY (ARRAY['cfpl'::text, 'cdpl'::text]))),
    CONSTRAINT production_indent_material_type_check CHECK ((material_type = ANY (ARRAY['FG'::text, 'SFG'::text]))),
    CONSTRAINT production_indent_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'submitted'::text, 'approved'::text, 'internal_jc_created'::text, 'fulfilled'::text, 'cancelled'::text])))
);


--
-- Name: production_indent_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.production_indent_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: production_indent_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.production_indent_id_seq OWNED BY public.production_indent.id;


--
-- Name: production_order; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.production_order (
    prod_order_id integer NOT NULL,
    prod_order_number text NOT NULL,
    plan_line_id integer,
    bom_id integer,
    fg_sku_name text NOT NULL,
    customer_name text,
    batch_number text NOT NULL,
    batch_size_kg numeric(15,3) NOT NULL,
    net_wt_per_unit numeric(15,3),
    best_before date,
    total_stages integer DEFAULT 1 NOT NULL,
    status text DEFAULT 'created'::text NOT NULL,
    entity text,
    factory text,
    floor text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    machine_id integer,
    CONSTRAINT production_order_entity_check CHECK ((entity = ANY (ARRAY['cfpl'::text, 'cdpl'::text])))
);


--
-- Name: production_order_prod_order_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.production_order_prod_order_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: production_order_prod_order_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.production_order_prod_order_id_seq OWNED BY public.production_order.prod_order_id;


--
-- Name: production_plan; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.production_plan (
    plan_id integer NOT NULL,
    plan_name text,
    entity text,
    plan_type text DEFAULT 'daily'::text NOT NULL,
    plan_date date NOT NULL,
    date_from date NOT NULL,
    date_to date NOT NULL,
    status text DEFAULT 'draft'::text NOT NULL,
    ai_generated boolean DEFAULT false,
    ai_analysis_json jsonb,
    revision_number integer DEFAULT 1,
    previous_plan_id integer,
    approved_by text,
    approved_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT production_plan_entity_check CHECK ((entity = ANY (ARRAY['cfpl'::text, 'cdpl'::text])))
);


--
-- Name: production_plan_line; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.production_plan_line (
    plan_line_id integer NOT NULL,
    plan_id integer NOT NULL,
    fg_sku_name text NOT NULL,
    customer_name text,
    bom_id integer,
    planned_qty_kg numeric(15,3) NOT NULL,
    planned_qty_units integer,
    machine_id integer,
    priority integer DEFAULT 5,
    shift text,
    stage_sequence text[],
    estimated_hours numeric(10,2),
    linked_so_fulfillment_ids integer[],
    reasoning text,
    status text DEFAULT 'planned'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    floor text
);


--
-- Name: production_plan_line_plan_line_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.production_plan_line_plan_line_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: production_plan_line_plan_line_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.production_plan_line_plan_line_id_seq OWNED BY public.production_plan_line.plan_line_id;


--
-- Name: production_plan_line_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.production_plan_line_v2 (
    plan_line_id bigint NOT NULL,
    plan_id bigint NOT NULL,
    fg_sku_name text NOT NULL,
    customer_name text,
    bom_id bigint NOT NULL,
    planned_qty_kg numeric(12,3) NOT NULL,
    planned_qty_units numeric(12,3) NOT NULL,
    area character varying(30),
    shift character varying(10),
    stage_sequence text[],
    estimated_hours numeric(8,2),
    linked_so_fulfillment_ids bigint[],
    status character varying(15) DEFAULT 'planned'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    deadline_date date,
    kg_was_overridden boolean DEFAULT false NOT NULL,
    override_reason text,
    CONSTRAINT production_plan_line_v2_estimated_hours_check CHECK ((estimated_hours >= (0)::numeric)),
    CONSTRAINT production_plan_line_v2_planned_qty_kg_check CHECK ((planned_qty_kg > (0)::numeric)),
    CONSTRAINT production_plan_line_v2_planned_qty_units_check CHECK ((planned_qty_units > (0)::numeric)),
    CONSTRAINT production_plan_line_v2_shift_check CHECK (((shift)::text = ANY ((ARRAY['A'::character varying, 'B'::character varying, 'C'::character varying, 'general'::character varying])::text[]))),
    CONSTRAINT production_plan_line_v2_status_check CHECK (((status)::text = ANY ((ARRAY['planned'::character varying, 'in_progress'::character varying, 'completed'::character varying, 'cancelled'::character varying])::text[])))
);


--
-- Name: TABLE production_plan_line_v2; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.production_plan_line_v2 IS 'v2 plan detail; one row per FG to produce. Links to BOM and to satisfying SO fulfillments.';


--
-- Name: COLUMN production_plan_line_v2.stage_sequence; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.production_plan_line_v2.stage_sequence IS 'Ordered list of process stage names from bom_process_route, snapshotted at plan creation.';


--
-- Name: COLUMN production_plan_line_v2.linked_so_fulfillment_ids; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.production_plan_line_v2.linked_so_fulfillment_ids IS 'Array of so_fulfillment_v2.so_fulfillment_id values this plan line is allocated against (M-to-N).';


--
-- Name: COLUMN production_plan_line_v2.deadline_date; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.production_plan_line_v2.deadline_date IS 'Per-line target completion date. NULLABLE — not all SO lines carry an explicit deadline (internal SOs, RM ad-hoc plans). Independent of the plan header date_to (which is the plan window upper bound).';


--
-- Name: production_plan_line_v2_plan_line_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.production_plan_line_v2_plan_line_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: production_plan_line_v2_plan_line_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.production_plan_line_v2_plan_line_id_seq OWNED BY public.production_plan_line_v2.plan_line_id;


--
-- Name: production_plan_plan_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.production_plan_plan_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: production_plan_plan_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.production_plan_plan_id_seq OWNED BY public.production_plan.plan_id;


--
-- Name: production_plan_step_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.production_plan_step_v2 (
    step_id bigint NOT NULL,
    plan_line_id bigint NOT NULL,
    step_order smallint NOT NULL,
    process_name text NOT NULL,
    stage text,
    floor character varying(30),
    std_time_min numeric(8,2),
    loss_pct numeric(5,3),
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT production_plan_step_v2_loss_pct_check CHECK (((loss_pct IS NULL) OR (loss_pct >= (0)::numeric))),
    CONSTRAINT production_plan_step_v2_std_time_min_check CHECK (((std_time_min IS NULL) OR (std_time_min >= (0)::numeric))),
    CONSTRAINT production_plan_step_v2_step_order_check CHECK ((step_order > 0))
);


--
-- Name: TABLE production_plan_step_v2; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.production_plan_step_v2 IS 'v2 per-plan-line ordered process steps. Initial rows are snapshotted from bom_process_route at plan creation; subsequent edits (reorder, floor change, add, delete) are scoped to this plan line only and do NOT affect the master BOM.';


--
-- Name: COLUMN production_plan_step_v2.step_order; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.production_plan_step_v2.step_order IS 'Position within the plan line, 1-based. Unique per plan_line_id. Constraint is DEFERRABLE so atomic reorders can pass through invalid intermediate states inside a transaction.';


--
-- Name: COLUMN production_plan_step_v2.floor; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.production_plan_step_v2.floor IS 'Free-text floor name. No FK; no constraint. Frontend supplies the value via plain text input.';


--
-- Name: production_plan_step_v2_step_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.production_plan_step_v2_step_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: production_plan_step_v2_step_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.production_plan_step_v2_step_id_seq OWNED BY public.production_plan_step_v2.step_id;


--
-- Name: production_plan_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.production_plan_v2 (
    plan_id bigint NOT NULL,
    entity character varying(10) NOT NULL,
    warehouse character varying(10) NOT NULL,
    plan_type character varying(10) NOT NULL,
    plan_date date NOT NULL,
    date_from date NOT NULL,
    date_to date NOT NULL,
    status character varying(15) DEFAULT 'draft'::character varying NOT NULL,
    revision_number smallint DEFAULT 0 NOT NULL,
    previous_plan_id bigint,
    approved_by text,
    approved_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT production_plan_v2_check CHECK ((date_to >= date_from)),
    CONSTRAINT production_plan_v2_check1 CHECK (((((status)::text = 'approved'::text) AND (approved_by IS NOT NULL) AND (approved_at IS NOT NULL)) OR ((status)::text <> 'approved'::text))),
    CONSTRAINT production_plan_v2_entity_check CHECK (((entity)::text = ANY ((ARRAY['cfpl'::character varying, 'cdpl'::character varying])::text[]))),
    CONSTRAINT production_plan_v2_plan_type_check CHECK (((plan_type)::text = ANY ((ARRAY['daily'::character varying, 'weekly'::character varying])::text[]))),
    CONSTRAINT production_plan_v2_revision_number_check CHECK ((revision_number >= 0)),
    CONSTRAINT production_plan_v2_status_check CHECK (((status)::text = ANY ((ARRAY['draft'::character varying, 'approved'::character varying, 'executed'::character varying, 'cancelled'::character varying])::text[]))),
    CONSTRAINT production_plan_v2_warehouse_check CHECK (((warehouse)::text = ANY ((ARRAY['W-202'::character varying, 'A-185'::character varying])::text[])))
);


--
-- Name: TABLE production_plan_v2; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.production_plan_v2 IS 'v2 plan header; one row per warehouse + plan_date. Revisions chain via previous_plan_id.';


--
-- Name: COLUMN production_plan_v2.revision_number; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.production_plan_v2.revision_number IS 'Starts at 0; incremented when a revised plan is created.';


--
-- Name: production_plan_v2_plan_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.production_plan_v2_plan_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: production_plan_v2_plan_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.production_plan_v2_plan_id_seq OWNED BY public.production_plan_v2.plan_id;


--
-- Name: purchase_indent; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.purchase_indent (
    indent_id integer NOT NULL,
    indent_number text NOT NULL,
    material_sku_name text NOT NULL,
    required_qty_kg numeric(15,3) NOT NULL,
    required_by_date date,
    priority integer DEFAULT 5,
    plan_line_id integer,
    po_reference text,
    status text DEFAULT 'raised'::text NOT NULL,
    acknowledged_by text,
    acknowledged_at timestamp with time zone,
    entity text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    store_allocation_id integer,
    job_card_id integer,
    indent_source text DEFAULT 'mrp'::text,
    customer_name text,
    so_reference text,
    triggered_by_batch text,
    shortfall_qty_kg numeric(15,3),
    cascade_from_indent_id integer,
    cascade_reason text,
    cancelled_at timestamp with time zone,
    cancelled_reason text,
    cascade_event_id integer,
    allocated_qty_kg numeric(15,3),
    allocated_by text,
    allocated_at timestamp with time zone,
    insufficient_reason text,
    CONSTRAINT purchase_indent_entity_check CHECK ((entity = ANY (ARRAY['cfpl'::text, 'cdpl'::text])))
);


--
-- Name: purchase_indent_indent_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.purchase_indent_indent_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: purchase_indent_indent_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.purchase_indent_indent_id_seq OWNED BY public.purchase_indent.indent_id;


--
-- Name: qc_inspection; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.qc_inspection (
    id integer NOT NULL,
    inspection_id text NOT NULL,
    job_card_id integer NOT NULL,
    jc_number text,
    fg_sku_name text,
    customer_name text,
    floor text,
    process_step text,
    checkpoint_type text NOT NULL,
    inspector_user text,
    inspection_date timestamp with time zone,
    result text DEFAULT 'pending'::text NOT NULL,
    findings text,
    corrective_action text,
    signed_off_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT qc_inspection_checkpoint_type_check CHECK ((checkpoint_type = ANY (ARRAY['pre_production'::text, 'in_process'::text, 'post_production'::text, 'rtv_disposition'::text]))),
    CONSTRAINT qc_inspection_result_check CHECK ((result = ANY (ARRAY['pending'::text, 'pass'::text, 'fail'::text, 'conditional_pass'::text])))
);


--
-- Name: qc_inspection_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.qc_inspection_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: qc_inspection_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.qc_inspection_id_seq OWNED BY public.qc_inspection.id;


--
-- Name: qc_intimation; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.qc_intimation (
    qc_intimation_id bigint NOT NULL,
    po_number text,
    transaction_no text,
    sku_id integer,
    sku_name text,
    sku_name_raw text,
    supplier_id integer,
    supplier_name text,
    lot_number text,
    vehicle_no text,
    warehouse text,
    entity text,
    status text DEFAULT 'pending'::text NOT NULL,
    coa_received boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    invoice_no text
);


--
-- Name: qc_inward_inspection; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.qc_inward_inspection (
    inspection_id bigint NOT NULL,
    inspection_ref text,
    qc_intimation_id bigint,
    status text DEFAULT 'in_progress'::text NOT NULL,
    verdict text,
    sample_size integer,
    inspection_method text,
    inspector_user_id integer,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    started_by integer,
    started_by_name text,
    verdict_at timestamp with time zone,
    accepted_qty numeric,
    rejected_qty numeric,
    ncr_no text,
    cancelled_at timestamp with time zone,
    cancelled_by integer,
    cancelled_by_name text,
    cancel_reason text,
    reopened_at timestamp with time zone,
    reopen_reason text,
    verdict_overridden_by integer,
    verdict_overridden_by_name text,
    override_reason text,
    remarks text,
    po_number text,
    transaction_no text,
    sku_id integer,
    sku_name text,
    sku_name_raw text,
    supplier_id integer,
    supplier_name text,
    lot_number text,
    vehicle_no text,
    warehouse text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    approved_by integer,
    approved_by_name text,
    approved_at timestamp with time zone
);


--
-- Name: qc_inward_inspection_audit; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.qc_inward_inspection_audit (
    audit_id bigint NOT NULL,
    inspection_id bigint NOT NULL,
    event_type text NOT NULL,
    from_state text,
    to_state text,
    actor_user_id integer,
    payload_diff jsonb,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: qc_inward_reading; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.qc_inward_reading (
    reading_id bigint NOT NULL,
    inspection_id bigint NOT NULL,
    parameter_id integer,
    parameter_name text,
    parameter_unit text,
    observed_value_num numeric,
    observed_value_text text,
    spec_min numeric,
    spec_max numeric,
    spec_target numeric,
    is_within_spec boolean,
    severity text,
    deviation_pct numeric,
    method text,
    instrument text,
    notes text,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: qc_notification_log_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.qc_notification_log_v2 (
    notification_id bigint NOT NULL,
    job_card_id bigint NOT NULL,
    recipient_user_id integer,
    dispatched_by integer,
    channel text DEFAULT 'hook'::text NOT NULL,
    note text,
    delivery_status text DEFAULT 'pending'::text NOT NULL,
    delivery_meta jsonb,
    dispatched_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT qc_notification_log_v2_delivery_status_check CHECK ((delivery_status = ANY (ARRAY['pending'::text, 'delivered'::text, 'failed'::text])))
);


--
-- Name: qc_parameter; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.qc_parameter (
    parameter_id integer NOT NULL,
    name text NOT NULL,
    unit text,
    code text,
    param_group text,
    data_type text,
    value_kind text,
    spec_note text,
    is_active boolean DEFAULT true NOT NULL,
    sort_order integer
);


--
-- Name: qc_parameter_parameter_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.qc_parameter_parameter_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: qc_parameter_parameter_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.qc_parameter_parameter_id_seq OWNED BY public.qc_parameter.parameter_id;


--
-- Name: qc_sku_spec; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.qc_sku_spec (
    sku_id integer NOT NULL,
    parameter_id integer NOT NULL,
    spec_min numeric,
    spec_max numeric,
    spec_target numeric
);


--
-- Name: quality_inspection; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.quality_inspection (
    inspection_id integer NOT NULL,
    job_card_id integer,
    inspection_type text NOT NULL,
    checkpoint text,
    result text NOT NULL,
    notes text,
    inspector_name text,
    entity text,
    inspected_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT quality_inspection_entity_check CHECK ((entity = ANY (ARRAY['cfpl'::text, 'cdpl'::text])))
);


--
-- Name: quality_inspection_inspection_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.quality_inspection_inspection_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: quality_inspection_inspection_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.quality_inspection_inspection_id_seq OWNED BY public.quality_inspection.inspection_id;


--
-- Name: receipt_document; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.receipt_document (
    document_id bigint NOT NULL,
    document_type text DEFAULT 'invoice'::text NOT NULL,
    transaction_no text,
    po_number text,
    dock_intimation_id integer,
    supplier_id integer,
    entity text,
    s3_key text NOT NULL,
    file_name text NOT NULL,
    file_size_bytes bigint NOT NULL,
    mime_type text NOT NULL,
    scan_status text DEFAULT 'pending'::text NOT NULL,
    remarks text,
    uploaded_by text,
    uploaded_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone,
    deleted_by text,
    delete_reason text,
    CONSTRAINT receipt_document_scan_status_check CHECK ((scan_status = ANY (ARRAY['pending'::text, 'clean'::text, 'infected'::text, 'skipped'::text]))),
    CONSTRAINT receipt_document_type_check CHECK ((document_type = ANY (ARRAY['invoice'::text, 'challan'::text, 'lr'::text, 'other'::text])))
);


--
-- Name: receipt_document_document_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.receipt_document_document_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: receipt_document_document_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.receipt_document_document_id_seq OWNED BY public.receipt_document.document_id;


--
-- Name: reconciliation_failures; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.reconciliation_failures (
    failure_id integer NOT NULL,
    sku_name text NOT NULL,
    entity text,
    expected_total numeric(15,3),
    actual_total numeric(15,3),
    discrepancy_kg numeric(15,3),
    status_breakdown jsonb,
    detected_at timestamp with time zone DEFAULT now() NOT NULL,
    resolved_at timestamp with time zone,
    resolved_by text,
    transaction_context text
);


--
-- Name: reconciliation_failures_failure_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.reconciliation_failures_failure_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: reconciliation_failures_failure_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.reconciliation_failures_failure_id_seq OWNED BY public.reconciliation_failures.failure_id;


--
-- Name: rm_issue_form; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.rm_issue_form (
    id integer NOT NULL,
    form_number text NOT NULL,
    trial_name text,
    product_name text,
    customer_name text,
    purpose_tag text,
    source_type text,
    source_id integer,
    requisition_id integer,
    entity text DEFAULT 'cfpl'::text NOT NULL,
    status text DEFAULT 'DRAFT'::text NOT NULL,
    requested_by integer,
    requested_at timestamp with time zone,
    issued_by integer,
    issued_at timestamp with time zone,
    issue_mat_doc_id text,
    cancellation_reason text,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT rm_issue_form_entity_check CHECK ((entity = ANY (ARRAY['cfpl'::text, 'cdpl'::text]))),
    CONSTRAINT rm_issue_form_status_check CHECK ((status = ANY (ARRAY['DRAFT'::text, 'SUBMITTED'::text, 'APPROVED'::text, 'ISSUED'::text, 'CLOSED'::text, 'CANCELLED'::text])))
);


--
-- Name: rm_issue_form_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.rm_issue_form_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: rm_issue_form_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.rm_issue_form_id_seq OWNED BY public.rm_issue_form.id;


--
-- Name: rm_issue_form_lines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.rm_issue_form_lines (
    id integer NOT NULL,
    form_id integer NOT NULL,
    sku_id integer,
    sku_name text NOT NULL,
    location text,
    lot_no text,
    reqd_qty numeric(15,3) DEFAULT 0 NOT NULL,
    issued_qty numeric(15,3),
    uom text DEFAULT 'kg'::text NOT NULL,
    ownership text DEFAULT 'OWN'::text NOT NULL,
    is_off_master boolean DEFAULT false NOT NULL,
    unit_cost numeric(15,4),
    notes text,
    line_order integer DEFAULT 0 NOT NULL,
    CONSTRAINT rm_issue_form_lines_ownership_check CHECK ((ownership = ANY (ARRAY['OWN'::text, 'CUSTOMER'::text])))
);


--
-- Name: rm_issue_form_lines_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.rm_issue_form_lines_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: rm_issue_form_lines_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.rm_issue_form_lines_id_seq OWNED BY public.rm_issue_form_lines.id;


--
-- Name: rtv_disposition; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.rtv_disposition (
    id integer NOT NULL,
    disposition_id text NOT NULL,
    rtv_id text NOT NULL,
    item_description text,
    qty numeric(12,2),
    net_weight numeric(12,3),
    source_type text DEFAULT 'RTV'::text,
    disposition_type text DEFAULT 'pending'::text NOT NULL,
    decided_by text,
    decided_at timestamp with time zone,
    qc_remarks text,
    linked_internal_order text,
    linked_offgrade_lot text,
    discard_approved boolean DEFAULT false,
    entity text DEFAULT 'cfpl'::text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT rtv_disposition_disposition_type_check CHECK ((disposition_type = ANY (ARRAY['pending'::text, 'reprocess'::text, 'offgrade'::text, 'discard'::text, 'return_to_vendor'::text]))),
    CONSTRAINT rtv_disposition_entity_check CHECK ((entity = ANY (ARRAY['cfpl'::text, 'cdpl'::text])))
);


--
-- Name: rtv_disposition_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.rtv_disposition_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: rtv_disposition_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.rtv_disposition_id_seq OWNED BY public.rtv_disposition.id;


--
-- Name: sample_approval_role_map; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sample_approval_role_map (
    id integer NOT NULL,
    approval_stage text NOT NULL,
    sample_type text,
    entity text DEFAULT '*'::text NOT NULL,
    required_role text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT sample_approval_role_map_approval_stage_check CHECK ((approval_stage = ANY (ARRAY['BH_APPROVAL'::text, 'PRODUCTION_ACK'::text, 'INV_MGR_VERIFICATION'::text, 'INV_MGR_SIGNOFF'::text, 'CONVERSION_APPROVAL'::text, 'CONVERSION_INV_MGR_SIGNOFF'::text]))),
    CONSTRAINT sample_approval_role_map_sample_type_check CHECK ((sample_type = ANY (ARRAY['BASIS_RM'::text, 'BASIS_FG'::text, 'NPD'::text, 'INTERNAL'::text, 'TRIAL'::text, '*'::text])))
);


--
-- Name: sample_approval_role_map_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.sample_approval_role_map_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: sample_approval_role_map_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.sample_approval_role_map_id_seq OWNED BY public.sample_approval_role_map.id;


--
-- Name: sample_approvals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sample_approvals (
    id integer NOT NULL,
    requisition_id integer NOT NULL,
    approval_stage text NOT NULL,
    approver_user_id integer NOT NULL,
    role_at_action text NOT NULL,
    action text DEFAULT 'PENDING'::text NOT NULL,
    remarks text,
    sequence_no integer NOT NULL,
    actioned_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT sample_approvals_action_check CHECK ((action = ANY (ARRAY['PENDING'::text, 'APPROVED'::text, 'REJECTED'::text, 'HOLD'::text]))),
    CONSTRAINT sample_approvals_approval_stage_check CHECK ((approval_stage = ANY (ARRAY['BH_APPROVAL'::text, 'PRODUCTION_ACK'::text, 'INV_MGR_VERIFICATION'::text, 'INV_MGR_SIGNOFF'::text, 'CONVERSION_APPROVAL'::text, 'CONVERSION_INV_MGR_SIGNOFF'::text])))
);


--
-- Name: sample_approvals_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.sample_approvals_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: sample_approvals_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.sample_approvals_id_seq OWNED BY public.sample_approvals.id;


--
-- Name: sample_audit_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sample_audit_log (
    id bigint NOT NULL,
    requisition_id integer NOT NULL,
    event_type text NOT NULL,
    old_value jsonb,
    new_value jsonb,
    actor_user_id integer,
    actor_role text,
    remarks text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: sample_audit_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.sample_audit_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: sample_audit_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.sample_audit_log_id_seq OWNED BY public.sample_audit_log.id;


--
-- Name: sample_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sample_config (
    config_key text NOT NULL,
    config_value text NOT NULL,
    description text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by integer
);


--
-- Name: sample_consumption_variance; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sample_consumption_variance (
    id bigint NOT NULL,
    source_type text NOT NULL,
    source_id integer NOT NULL,
    material_sku_name text NOT NULL,
    required_qty numeric(15,3) NOT NULL,
    issued_qty numeric(15,3) NOT NULL,
    variance_qty numeric(15,3) GENERATED ALWAYS AS ((issued_qty - required_qty)) STORED,
    uom text DEFAULT 'kg'::text NOT NULL,
    unit_cost numeric(15,4),
    variance_cost numeric(15,2),
    recorded_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: sample_consumption_variance_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.sample_consumption_variance_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: sample_consumption_variance_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.sample_consumption_variance_id_seq OWNED BY public.sample_consumption_variance.id;


--
-- Name: sample_notification_map; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sample_notification_map (
    id integer NOT NULL,
    event text NOT NULL,
    sample_type text DEFAULT '*'::text NOT NULL,
    notify_teams text[] DEFAULT '{}'::text[] NOT NULL,
    mail_template text,
    mail_enabled boolean DEFAULT true NOT NULL,
    requires_maker_checker boolean DEFAULT false NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: sample_notification_map_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.sample_notification_map_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: sample_notification_map_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.sample_notification_map_id_seq OWNED BY public.sample_notification_map.id;


--
-- Name: sample_requisition_articles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sample_requisition_articles (
    id integer NOT NULL,
    requisition_id integer NOT NULL,
    sku_id integer NOT NULL,
    sku_name text NOT NULL,
    required_qty numeric NOT NULL,
    issued_qty numeric,
    uom text NOT NULL,
    article_role text NOT NULL,
    pack_size_kg numeric,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT sample_requisition_articles_article_role_check CHECK ((article_role = ANY (ARRAY['RM'::text, 'FG'::text, 'NPD_INPUT'::text, 'NPD_OUTPUT'::text]))),
    CONSTRAINT sample_requisition_articles_issued_qty_check CHECK ((issued_qty >= (0)::numeric)),
    CONSTRAINT sample_requisition_articles_required_qty_check CHECK ((required_qty > (0)::numeric))
);


--
-- Name: sample_requisition_articles_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.sample_requisition_articles_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: sample_requisition_articles_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.sample_requisition_articles_id_seq OWNED BY public.sample_requisition_articles.id;


--
-- Name: sample_requisitions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sample_requisitions (
    id integer NOT NULL,
    sample_type text NOT NULL,
    status text DEFAULT 'DRAFT'::text NOT NULL,
    requestor_user_id integer NOT NULL,
    requestor_team text,
    business_head_user_id integer,
    purpose_tag text,
    purpose_note text,
    base_bom_id integer,
    npd_draft_bom_id integer,
    linked_job_card_id integer,
    linked_gate_pass_id integer,
    converted_from_id integer,
    internal_override boolean DEFAULT false NOT NULL,
    converted_to_external boolean DEFAULT false NOT NULL,
    warehouse text NOT NULL,
    cancellation_reason text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by integer,
    updated_by integer,
    deleted_at timestamp with time zone,
    deleted_by integer,
    transporter_name text,
    vehicle_number text,
    fg_sample_batch_id text,
    npd_target_name text,
    quantity numeric(15,3),
    request_id bigint NOT NULL,
    linked_dev_jc_id bigint,
    description text,
    hold_start_date date,
    company_name text,
    customer_name text,
    customer_contact text,
    customer_ship_to_address text,
    mode_of_transport text,
    expected_dispatch_date date,
    confirmed_dispatch_date date,
    pcs numeric(15,3),
    weight_per_piece numeric(15,4),
    email_thread_msgid text,
    last_reminder_at timestamp with time zone,
    reminder_count integer DEFAULT 0 NOT NULL,
    returnable boolean DEFAULT false NOT NULL,
    non_returnable boolean DEFAULT false NOT NULL,
    paid boolean DEFAULT false NOT NULL,
    amount numeric(12,2) DEFAULT 0 NOT NULL,
    CONSTRAINT chk_sample_req_paid_amount CHECK (((paid AND (amount > (0)::numeric)) OR ((NOT paid) AND (amount = (0)::numeric)))),
    CONSTRAINT chk_sample_req_return_xor CHECK ((NOT (returnable AND non_returnable))),
    CONSTRAINT sample_requisitions_purpose_tag_check CHECK ((purpose_tag = ANY (ARRAY['CUSTOMER_DISPLAY'::text, 'CUSTOMER_ISSUE'::text, 'TASTING_SENSORY'::text, 'PHYSICAL_PARAMETERS'::text, 'INTERNAL_OTHER'::text]))),
    CONSTRAINT sample_requisitions_sample_type_check CHECK ((sample_type = ANY (ARRAY['BASIS_RM'::text, 'BASIS_FG'::text, 'NPD'::text, 'INTERNAL'::text, 'TRIAL'::text]))),
    CONSTRAINT sample_requisitions_status_check CHECK ((status = ANY (ARRAY['DRAFT'::text, 'SUBMITTED'::text, 'BH_APPROVED'::text, 'BH_REJECTED'::text, 'ON_HOLD'::text, 'IN_PRODUCTION'::text, 'PACKING'::text, 'READY_FOR_DISPATCH'::text, 'INTERNALLY_DISPATCHED'::text, 'PARTIALLY_CONVERTED'::text, 'GATE_PASS_ISSUED'::text, 'CLOSED'::text, 'CANCELLED'::text]))),
    CONSTRAINT sample_requisitions_warehouse_check CHECK ((warehouse = ANY (ARRAY['W202'::text, 'A185'::text, 'A68'::text, 'F53'::text, 'A101'::text, 'D-39'::text, 'D-514'::text, 'Rishi'::text, 'Supreme'::text])))
);


--
-- Name: sample_requisitions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.sample_requisitions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: sample_requisitions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.sample_requisitions_id_seq OWNED BY public.sample_requisitions.id;


--
-- Name: seq_block; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.seq_block
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: seq_gate_pass; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.seq_gate_pass
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: seq_iet; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.seq_iet
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: seq_int_jc; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.seq_int_jc
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: seq_int_ord; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.seq_int_ord
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: seq_isn; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.seq_isn
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: seq_matdoc; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.seq_matdoc
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: seq_ogi; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.seq_ogi
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: seq_prdi; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.seq_prdi
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: seq_qci; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.seq_qci
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: seq_rm_issue_form; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.seq_rm_issue_form
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: seq_rtvd; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.seq_rtvd
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: sfg_box; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sfg_box (
    box_id bigint NOT NULL,
    job_card_id bigint NOT NULL,
    job_card_number text,
    sfg_code text NOT NULL,
    entity text,
    floor text,
    stage_bucket text,
    box_number integer NOT NULL,
    total_boxes integer NOT NULL,
    net_weight numeric(15,3) NOT NULL,
    gross_weight numeric(15,3),
    status text DEFAULT 'PRINTED'::text NOT NULL,
    source_inventory_batch_id text,
    received_into_job_card_id bigint,
    lot_number text,
    parent_box_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT chk_sfg_box_number CHECK (((box_number >= 1) AND (box_number <= total_boxes))),
    CONSTRAINT chk_sfg_box_status CHECK ((status = ANY (ARRAY['PRINTED'::text, 'DISPATCHED'::text, 'RECEIVED'::text, 'CONSUMED'::text]))),
    CONSTRAINT uq_sfg_box_jc_boxnum UNIQUE (job_card_id, box_number)
);


--
-- Name: TABLE sfg_box; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.sfg_box IS 'Physical SFG boxes/bags produced at a WIP-stage completion. QR(box_id) printed and scan-verified between floors/stages/units (mirror of po_box). SUM(net_weight) per job_card_id reconciles to that JC''s WIP inventory_batch. Batch/lot genealogy deferred.';


--
-- Name: so_fulfillment; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.so_fulfillment (
    fulfillment_id integer NOT NULL,
    so_line_id bigint NOT NULL,
    so_id integer,
    financial_year text NOT NULL,
    fg_sku_name text NOT NULL,
    customer_name text,
    original_qty_kg numeric(15,3) NOT NULL,
    revised_qty_kg numeric(15,3),
    pending_qty_kg numeric(15,3) NOT NULL,
    produced_qty_kg numeric(15,3) DEFAULT 0,
    dispatched_qty_kg numeric(15,3) DEFAULT 0,
    order_status text DEFAULT 'open'::text NOT NULL,
    delivery_deadline date,
    priority integer DEFAULT 5,
    carryforward_from_id integer,
    entity text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT so_fulfillment_entity_check CHECK ((entity = ANY (ARRAY['cfpl'::text, 'cdpl'::text])))
);


--
-- Name: so_fulfillment_fulfillment_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.so_fulfillment_fulfillment_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: so_fulfillment_fulfillment_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.so_fulfillment_fulfillment_id_seq OWNED BY public.so_fulfillment.fulfillment_id;


--
-- Name: so_fulfillment_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.so_fulfillment_v2 (
    so_fulfillment_id bigint NOT NULL,
    so_line_id bigint NOT NULL,
    financial_year character varying(7) NOT NULL,
    fg_sku_name text NOT NULL,
    customer_name text NOT NULL,
    entity character varying(10) NOT NULL,
    original_qty_kg numeric(12,3) NOT NULL,
    produced_qty_kg numeric(12,3) DEFAULT 0 NOT NULL,
    dispatched_qty_kg numeric(12,3) DEFAULT 0 NOT NULL,
    original_qty_units numeric(12,3),
    produced_qty_units numeric(12,3) DEFAULT 0 NOT NULL,
    dispatched_qty_units numeric(12,3) DEFAULT 0 NOT NULL,
    deadline_date date,
    order_status character varying(20) DEFAULT 'open'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    carryforward_from_id bigint,
    cancelled_by text,
    cancelled_at timestamp with time zone,
    cancellation_reason text,
    planned_qty_kg numeric(12,3) DEFAULT 0 NOT NULL,
    planned_qty_units numeric(12,3) DEFAULT 0 NOT NULL,
    pending_qty_kg numeric(12,3) GENERATED ALWAYS AS (((original_qty_kg - dispatched_qty_kg) - planned_qty_kg)) STORED,
    pending_qty_units numeric(12,3) GENERATED ALWAYS AS (((original_qty_units - dispatched_qty_units) - planned_qty_units)) STORED,
    CONSTRAINT chk_pending_kg_nonneg CHECK ((((original_qty_kg - dispatched_qty_kg) - planned_qty_kg) >= (0)::numeric)),
    CONSTRAINT chk_pending_units_nonneg CHECK (((original_qty_units IS NULL) OR (((original_qty_units - COALESCE(dispatched_qty_units, (0)::numeric)) - COALESCE(planned_qty_units, (0)::numeric)) >= (0)::numeric))),
    CONSTRAINT so_fulfillment_v2_dispatched_qty_kg_check CHECK ((dispatched_qty_kg >= (0)::numeric)),
    CONSTRAINT so_fulfillment_v2_dispatched_qty_units_check CHECK ((dispatched_qty_units >= (0)::numeric)),
    CONSTRAINT so_fulfillment_v2_entity_check CHECK (((entity)::text = ANY ((ARRAY['cfpl'::character varying, 'cdpl'::character varying])::text[]))),
    CONSTRAINT so_fulfillment_v2_order_status_check CHECK (((order_status)::text = ANY ((ARRAY['open'::character varying, 'partial'::character varying, 'fulfilled'::character varying, 'carryforward'::character varying, 'cancelled'::character varying])::text[]))),
    CONSTRAINT so_fulfillment_v2_original_qty_kg_check CHECK ((original_qty_kg >= (0)::numeric)),
    CONSTRAINT so_fulfillment_v2_original_qty_units_check CHECK (((original_qty_units IS NULL) OR (original_qty_units >= (0)::numeric))),
    CONSTRAINT so_fulfillment_v2_planned_qty_kg_check CHECK ((planned_qty_kg >= (0)::numeric)),
    CONSTRAINT so_fulfillment_v2_planned_qty_units_check CHECK ((planned_qty_units >= (0)::numeric)),
    CONSTRAINT so_fulfillment_v2_produced_qty_kg_check CHECK ((produced_qty_kg >= (0)::numeric)),
    CONSTRAINT so_fulfillment_v2_produced_qty_units_check CHECK ((produced_qty_units >= (0)::numeric))
);


--
-- Name: TABLE so_fulfillment_v2; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.so_fulfillment_v2 IS 'v2 demand-side ledger fed from external SO module; carries running pending qty per SO line.';


--
-- Name: COLUMN so_fulfillment_v2.carryforward_from_id; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.so_fulfillment_v2.carryforward_from_id IS 'Self-ref FK to prior FY''s row when this row is created via FY rollover.';


--
-- Name: COLUMN so_fulfillment_v2.cancelled_by; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.so_fulfillment_v2.cancelled_by IS 'User who cancelled the order. NULL unless order_status = ''cancelled''.';


--
-- Name: COLUMN so_fulfillment_v2.planned_qty_kg; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.so_fulfillment_v2.planned_qty_kg IS 'Kg cumulatively committed to active (non-cancelled) production plans. Bumped by plan_v2.create_plan; released by plan_v2.cancel_plan. Subtracted from pending_qty_kg via the generated column.';


--
-- Name: COLUMN so_fulfillment_v2.planned_qty_units; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.so_fulfillment_v2.planned_qty_units IS 'Pack count cumulatively committed to active plans. Mirror of planned_qty_kg.';


--
-- Name: COLUMN so_fulfillment_v2.pending_qty_kg; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.so_fulfillment_v2.pending_qty_kg IS 'Auto-computed: original_qty_kg - dispatched_qty_kg - planned_qty_kg. Hard-bound to >= 0 via chk_pending_kg_nonneg.';


--
-- Name: COLUMN so_fulfillment_v2.pending_qty_units; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON COLUMN public.so_fulfillment_v2.pending_qty_units IS 'Auto-computed: original_qty_units - dispatched_qty_units - planned_qty_units. NULL when original_qty_units is NULL.';


--
-- Name: so_gst_reconciliation; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.so_gst_reconciliation (
    recon_id integer NOT NULL,
    so_line_id bigint NOT NULL,
    so_id integer NOT NULL,
    expected_gst_rate numeric(15,3),
    actual_gst_rate numeric(15,3),
    expected_gst_amount numeric(15,3),
    actual_gst_amount numeric(15,3),
    gst_difference numeric(15,3),
    gst_type text,
    gst_type_valid boolean,
    sgst_cgst_equal boolean,
    total_with_gst_valid boolean,
    uom_match boolean,
    item_type_flag text,
    rate_type text,
    matched_item_description text,
    matched_item_type text,
    matched_item_category text,
    matched_sub_category text,
    matched_sales_group text,
    matched_uom numeric(15,3),
    match_score numeric(15,3),
    status text DEFAULT 'ok'::text NOT NULL,
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: so_gst_reconciliation_recon_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.so_gst_reconciliation_recon_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: so_gst_reconciliation_recon_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.so_gst_reconciliation_recon_id_seq OWNED BY public.so_gst_reconciliation.recon_id;


--
-- Name: so_header; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.so_header (
    so_id integer NOT NULL,
    so_number text,
    so_date date,
    customer_name text,
    common_customer_name text,
    company text,
    voucher_type text,
    extraction_status text DEFAULT 'pending'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: so_header_so_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.so_header_so_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: so_header_so_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.so_header_so_id_seq OWNED BY public.so_header.so_id;


--
-- Name: so_line; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.so_line (
    so_line_id bigint NOT NULL,
    so_id integer NOT NULL,
    line_number integer NOT NULL,
    sku_name text,
    item_category text,
    sub_category text,
    uom text,
    grp_code text,
    quantity numeric(15,3),
    quantity_units integer,
    rate_inr numeric(15,3),
    rate_type text,
    amount_inr numeric(15,3),
    igst_amount numeric(15,3),
    sgst_amount numeric(15,3),
    cgst_amount numeric(15,3),
    total_amount_inr numeric(15,3),
    apmc_amount numeric(15,3),
    packing_amount numeric(15,3),
    freight_amount numeric(15,3),
    processing_amount numeric(15,3),
    item_type text,
    item_description text,
    sales_group text,
    match_score numeric(15,3),
    match_source text,
    release_mode text DEFAULT 'all_upfront'::text,
    status text DEFAULT 'pending'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: so_revision_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.so_revision_log (
    revision_id integer NOT NULL,
    fulfillment_id integer NOT NULL,
    revision_type text NOT NULL,
    old_value text,
    new_value text,
    reason text,
    revised_by text,
    revised_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: so_revision_log_revision_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.so_revision_log_revision_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: so_revision_log_revision_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.so_revision_log_revision_id_seq OWNED BY public.so_revision_log.revision_id;


--
-- Name: so_revision_log_v2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.so_revision_log_v2 (
    revision_id bigint NOT NULL,
    so_fulfillment_id bigint NOT NULL,
    revision_type character varying(20) NOT NULL,
    old_value text,
    new_value text,
    reason text,
    revised_by text,
    revised_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT so_revision_log_v2_revision_type_check CHECK (((revision_type)::text = ANY ((ARRAY['qty_change'::character varying, 'units_change'::character varying, 'date_change'::character varying, 'carryforward'::character varying, 'cancel'::character varying, 'bom_override'::character varying])::text[])))
);


--
-- Name: TABLE so_revision_log_v2; Type: COMMENT; Schema: public; Owner: -
--

COMMENT ON TABLE public.so_revision_log_v2 IS 'Audit trail for qty and deadline revisions on so_fulfillment_v2 rows.';


--
-- Name: so_revision_log_v2_revision_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.so_revision_log_v2_revision_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: so_revision_log_v2_revision_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.so_revision_log_v2_revision_id_seq OWNED BY public.so_revision_log_v2.revision_id;


--
-- Name: store_alert; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.store_alert (
    alert_id integer NOT NULL,
    alert_type text NOT NULL,
    target_team text NOT NULL,
    message text NOT NULL,
    related_id integer,
    related_type text,
    is_read boolean DEFAULT false,
    entity text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT store_alert_entity_check CHECK ((entity = ANY (ARRAY['cfpl'::text, 'cdpl'::text])))
);


--
-- Name: store_alert_alert_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.store_alert_alert_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: store_alert_alert_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.store_alert_alert_id_seq OWNED BY public.store_alert.alert_id;


--
-- Name: store_allocation; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.store_allocation (
    allocation_id integer NOT NULL,
    job_card_id integer NOT NULL,
    indent_type text NOT NULL,
    indent_id integer NOT NULL,
    material_sku_name text NOT NULL,
    reqd_qty numeric(15,3) NOT NULL,
    approved_qty numeric(15,3) DEFAULT 0,
    rejected_qty numeric(15,3) DEFAULT 0,
    decision text DEFAULT 'pending'::text NOT NULL,
    rejection_reason text,
    rejection_detail text,
    reserved_for_customer text,
    quality_grade_available text,
    quality_grade_required text,
    expiry_date date,
    suggested_alternative_id integer,
    suggested_alternative_qty numeric(15,3),
    purchase_indent_id integer,
    floor_stock_verified boolean DEFAULT false,
    floor_stock_qty numeric(15,3),
    source_location text,
    decided_by text,
    decided_at timestamp with time zone,
    entity text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT store_allocation_entity_check CHECK ((entity = ANY (ARRAY['cfpl'::text, 'cdpl'::text])))
);


--
-- Name: store_allocation_allocation_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.store_allocation_allocation_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: store_allocation_allocation_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.store_allocation_allocation_id_seq OWNED BY public.store_allocation.allocation_id;


--
-- Name: transfer_request_lines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.transfer_request_lines (
    id bigint NOT NULL,
    request_id bigint,
    item_category text,
    qty numeric(15,3),
    net_weight numeric(15,3)
);


--
-- Name: transfer_requests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.transfer_requests (
    id bigint NOT NULL,
    request_no text,
    from_site text,
    to_site text,
    status text,
    reason_code text
);


--
-- Name: vendor_banking; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vendor_banking (
    bank_id bigint NOT NULL,
    vendor_id character varying(32),
    account_no text,
    is_primary boolean
);


--
-- Name: vendor_banking_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vendor_banking_history (
    history_id bigint NOT NULL,
    bank_id character varying(32) NOT NULL,
    vendor_id character varying(32) NOT NULL,
    operation character varying(16) NOT NULL,
    changed_by character varying(64),
    changed_at timestamp with time zone DEFAULT now() NOT NULL,
    previous_state jsonb,
    new_state jsonb NOT NULL,
    diff jsonb DEFAULT '{}'::jsonb NOT NULL,
    source character varying(32) DEFAULT 'manual'::character varying NOT NULL,
    reason text,
    CONSTRAINT vendor_banking_history_operation_check CHECK (((operation)::text = ANY ((ARRAY['create'::character varying, 'update'::character varying, 'delete'::character varying, 'set_primary'::character varying])::text[])))
);


--
-- Name: vendor_contract; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vendor_contract (
    contract_id bigint NOT NULL,
    vendor_id character varying(32),
    contract_type text
);


--
-- Name: vendor_contract_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vendor_contract_history (
    history_id bigint NOT NULL,
    contract_id character varying(32) NOT NULL,
    vendor_id character varying(32) NOT NULL,
    operation character varying(16) NOT NULL,
    changed_by character varying(64),
    changed_at timestamp with time zone DEFAULT now() NOT NULL,
    previous_state jsonb,
    new_state jsonb NOT NULL,
    diff jsonb DEFAULT '{}'::jsonb NOT NULL,
    source character varying(32) DEFAULT 'manual'::character varying NOT NULL,
    reason text,
    CONSTRAINT vendor_contract_history_operation_check CHECK (((operation)::text = ANY ((ARRAY['create'::character varying, 'update'::character varying, 'delete'::character varying, 'append_file'::character varying])::text[])))
);


--
-- Name: vendor_document; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vendor_document (
    doc_id bigint NOT NULL,
    vendor_id character varying(32),
    doc_type text,
    valid_to date
);


--
-- Name: vendor_document_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vendor_document_history (
    history_id bigint NOT NULL,
    doc_id character varying(32) NOT NULL,
    vendor_id character varying(32) NOT NULL,
    operation character varying(16) NOT NULL,
    changed_by character varying(64),
    changed_at timestamp with time zone DEFAULT now() NOT NULL,
    previous_state jsonb,
    new_state jsonb NOT NULL,
    diff jsonb DEFAULT '{}'::jsonb NOT NULL,
    source character varying(32) DEFAULT 'manual'::character varying NOT NULL,
    reason text,
    CONSTRAINT vendor_document_history_operation_check CHECK (((operation)::text = ANY ((ARRAY['create'::character varying, 'update'::character varying, 'delete'::character varying, 'append_file'::character varying])::text[])))
);


--
-- Name: vendor_extraction_staging; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vendor_extraction_staging (
    staging_id character varying(40) NOT NULL,
    created_by character varying(64) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    payload jsonb NOT NULL,
    s3_staging_prefix character varying(256) NOT NULL,
    expires_at timestamp with time zone DEFAULT (now() + '24:00:00'::interval) NOT NULL,
    consumed_at timestamp with time zone,
    consumed_vendor_id character varying(32)
);


--
-- Name: vendor_master; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vendor_master (
    vendor_id character varying(32) NOT NULL,
    supplier_code text,
    name text,
    status text,
    created_by text
);


--
-- Name: vendor_master_history; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.vendor_master_history (
    history_id bigint NOT NULL,
    vendor_id character varying(32) NOT NULL,
    operation character varying(16) NOT NULL,
    changed_by character varying(64),
    changed_at timestamp with time zone DEFAULT now() NOT NULL,
    previous_state jsonb,
    new_state jsonb NOT NULL,
    diff jsonb DEFAULT '{}'::jsonb NOT NULL,
    source character varying(32) DEFAULT 'manual'::character varying NOT NULL,
    reason text,
    CONSTRAINT vendor_master_history_operation_check CHECK (((operation)::text = ANY ((ARRAY['create'::character varying, 'update'::character varying, 'approve'::character varying, 'delete'::character varying, 'restore'::character varying, 'revert'::character varying])::text[])))
);


--
-- Name: wa_pending_action; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.wa_pending_action (
    wa_phone text NOT NULL,
    requisition_id integer NOT NULL,
    action text DEFAULT 'HOLD'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT wa_pending_action_action_check CHECK ((action = 'HOLD'::text))
);


--
-- Name: wa_promote_message; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.wa_promote_message (
    wamid text NOT NULL,
    dev_jc_id bigint NOT NULL,
    approver_kind text NOT NULL,
    wa_phone text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT wa_promote_message_approver_kind_check CHECK ((approver_kind = ANY (ARRAY['INV_MGR'::text, 'REQUESTOR_BH'::text])))
);


--
-- Name: wa_promote_pending; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.wa_promote_pending (
    wa_phone text NOT NULL,
    dev_jc_id bigint NOT NULL,
    approver_kind text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT wa_promote_pending_approver_kind_check CHECK ((approver_kind = ANY (ARRAY['INV_MGR'::text, 'REQUESTOR_BH'::text])))
);


--
-- Name: wa_review_message; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.wa_review_message (
    wamid text NOT NULL,
    requisition_id integer NOT NULL,
    kind text DEFAULT 'REVIEW'::text NOT NULL,
    wa_phone text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT wa_review_message_kind_check CHECK ((kind = ANY (ARRAY['REVIEW'::text, 'UPDATED'::text])))
);


--
-- Name: warehouse_sites; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.warehouse_sites (
    site_code text NOT NULL,
    site_name text,
    active boolean
);


--
-- Name: webhook_delivery; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.webhook_delivery (
    id bigint NOT NULL,
    endpoint_id integer NOT NULL,
    event_type text NOT NULL,
    event_id text NOT NULL,
    payload jsonb NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    last_attempt_at timestamp with time zone,
    response_code integer,
    response_body text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    target_roles text[] DEFAULT '{}'::text[] NOT NULL
);


--
-- Name: webhook_delivery_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.webhook_delivery_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: webhook_delivery_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.webhook_delivery_id_seq OWNED BY public.webhook_delivery.id;


--
-- Name: webhook_endpoint; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.webhook_endpoint (
    id integer NOT NULL,
    entity text NOT NULL,
    url text NOT NULL,
    secret text NOT NULL,
    description text,
    is_active boolean DEFAULT true NOT NULL,
    created_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: webhook_endpoint_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.webhook_endpoint_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: webhook_endpoint_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.webhook_endpoint_id_seq OWNED BY public.webhook_endpoint.id;


--
-- Name: webhook_subscription; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.webhook_subscription (
    id integer NOT NULL,
    endpoint_id integer NOT NULL,
    event_type text NOT NULL,
    filter_jsonb jsonb DEFAULT '{}'::jsonb,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: webhook_subscription_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.webhook_subscription_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: webhook_subscription_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.webhook_subscription_id_seq OWNED BY public.webhook_subscription.id;


--
-- Name: write_off_ledger; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.write_off_ledger (
    id integer NOT NULL,
    rtv_id text,
    offgrade_id text,
    item_description text NOT NULL,
    lot_number text,
    qty numeric(12,2),
    net_weight numeric(12,3),
    reason text NOT NULL,
    authorised_by text NOT NULL,
    written_off_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: write_off_ledger_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.write_off_ledger_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: write_off_ledger_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.write_off_ledger_id_seq OWNED BY public.write_off_ledger.id;


--
-- Name: yield_summary; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.yield_summary (
    yield_id integer NOT NULL,
    product_name text NOT NULL,
    item_group text,
    period text NOT NULL,
    total_input_kg numeric(15,3) NOT NULL,
    total_output_kg numeric(15,3) NOT NULL,
    yield_pct numeric(5,3),
    total_loss_kg numeric(15,3),
    total_offgrade_kg numeric(15,3),
    entity text,
    computed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT yield_summary_entity_check CHECK ((entity = ANY (ARRAY['cfpl'::text, 'cdpl'::text])))
);


--
-- Name: yield_summary_yield_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.yield_summary_yield_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: yield_summary_yield_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.yield_summary_yield_id_seq OWNED BY public.yield_summary.yield_id;


--
-- Name: ai_recommendation recommendation_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_recommendation ALTER COLUMN recommendation_id SET DEFAULT nextval('public.ai_recommendation_recommendation_id_seq'::regclass);


--
-- Name: all_sku sku_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.all_sku ALTER COLUMN sku_id SET DEFAULT nextval('public.all_sku_sku_id_seq'::regclass);


--
-- Name: amendment_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.amendment_log ALTER COLUMN id SET DEFAULT nextval('public.amendment_log_id_seq'::regclass);


--
-- Name: auth_permission permission_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_permission ALTER COLUMN permission_id SET DEFAULT nextval('public.auth_permission_permission_id_seq'::regclass);


--
-- Name: auth_role role_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_role ALTER COLUMN role_id SET DEFAULT nextval('public.auth_role_role_id_seq'::regclass);


--
-- Name: auth_session session_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_session ALTER COLUMN session_id SET DEFAULT nextval('public.auth_session_session_id_seq'::regclass);


--
-- Name: auth_user user_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_user ALTER COLUMN user_id SET DEFAULT nextval('public.auth_user_user_id_seq'::regclass);


--
-- Name: batch_block_history id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.batch_block_history ALTER COLUMN id SET DEFAULT nextval('public.batch_block_history_id_seq'::regclass);


--
-- Name: batch_rejection_log log_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.batch_rejection_log ALTER COLUMN log_id SET DEFAULT nextval('public.batch_rejection_log_log_id_seq'::regclass);


--
-- Name: bom_header bom_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bom_header ALTER COLUMN bom_id SET DEFAULT nextval('public.bom_header_bom_id_seq'::regclass);


--
-- Name: bom_line bom_line_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bom_line ALTER COLUMN bom_line_id SET DEFAULT nextval('public.bom_line_bom_line_id_seq'::regclass);


--
-- Name: bom_process_route route_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bom_process_route ALTER COLUMN route_id SET DEFAULT nextval('public.bom_process_route_route_id_seq'::regclass);


--
-- Name: cascade_events event_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cascade_events ALTER COLUMN event_id SET DEFAULT nextval('public.cascade_events_event_id_seq'::regclass);


--
-- Name: day_end_balance_scan scan_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.day_end_balance_scan ALTER COLUMN scan_id SET DEFAULT nextval('public.day_end_balance_scan_scan_id_seq'::regclass);


--
-- Name: day_end_balance_scan_line scan_line_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.day_end_balance_scan_line ALTER COLUMN scan_line_id SET DEFAULT nextval('public.day_end_balance_scan_line_scan_line_id_seq'::regclass);


--
-- Name: discrepancy_report discrepancy_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.discrepancy_report ALTER COLUMN discrepancy_id SET DEFAULT nextval('public.discrepancy_report_discrepancy_id_seq'::regclass);


--
-- Name: fifo_skip_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fifo_skip_log ALTER COLUMN id SET DEFAULT nextval('public.fifo_skip_log_id_seq'::regclass);


--
-- Name: floor_inventory inventory_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.floor_inventory ALTER COLUMN inventory_id SET DEFAULT nextval('public.floor_inventory_inventory_id_seq'::regclass);


--
-- Name: floor_movement movement_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.floor_movement ALTER COLUMN movement_id SET DEFAULT nextval('public.floor_movement_movement_id_seq'::regclass);


--
-- Name: fulfillment_bom_override override_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fulfillment_bom_override ALTER COLUMN override_id SET DEFAULT nextval('public.fulfillment_bom_override_override_id_seq'::regclass);


--
-- Name: fulfillment_floor_stock floor_stock_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fulfillment_floor_stock ALTER COLUMN floor_stock_id SET DEFAULT nextval('public.fulfillment_floor_stock_floor_stock_id_seq'::regclass);


--
-- Name: gate_passes id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gate_passes ALTER COLUMN id SET DEFAULT nextval('public.gate_passes_id_seq'::regclass);


--
-- Name: inter_entity_transfer id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inter_entity_transfer ALTER COLUMN id SET DEFAULT nextval('public.inter_entity_transfer_id_seq'::regclass);


--
-- Name: inter_entity_transfer_line id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inter_entity_transfer_line ALTER COLUMN id SET DEFAULT nextval('public.inter_entity_transfer_line_id_seq'::regclass);


--
-- Name: internal_issue_note note_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.internal_issue_note ALTER COLUMN note_id SET DEFAULT nextval('public.internal_issue_note_note_id_seq'::regclass);


--
-- Name: internal_order id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.internal_order ALTER COLUMN id SET DEFAULT nextval('public.internal_order_id_seq'::regclass);


--
-- Name: inventory_event_log event_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inventory_event_log ALTER COLUMN event_id SET DEFAULT nextval('public.inventory_event_log_event_id_seq'::regclass);


--
-- Name: issue_note id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.issue_note ALTER COLUMN id SET DEFAULT nextval('public.issue_note_id_seq'::regclass);


--
-- Name: issue_note_line id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.issue_note_line ALTER COLUMN id SET DEFAULT nextval('public.issue_note_line_id_seq'::regclass);


--
-- Name: job_card_balance_material balance_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_balance_material ALTER COLUMN balance_id SET DEFAULT nextval('public.job_card_balance_material_balance_id_seq'::regclass);


--
-- Name: job_card_environment env_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_environment ALTER COLUMN env_id SET DEFAULT nextval('public.job_card_environment_env_id_seq'::regclass);


--
-- Name: job_card_loss_reconciliation recon_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_loss_reconciliation ALTER COLUMN recon_id SET DEFAULT nextval('public.job_card_loss_reconciliation_recon_id_seq'::regclass);


--
-- Name: job_card_material_consumption consumption_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_material_consumption ALTER COLUMN consumption_id SET DEFAULT nextval('public.job_card_material_consumption_consumption_id_seq'::regclass);


--
-- Name: job_card_metal_detection detection_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_metal_detection ALTER COLUMN detection_id SET DEFAULT nextval('public.job_card_metal_detection_detection_id_seq'::regclass);


--
-- Name: job_card_output output_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_output ALTER COLUMN output_id SET DEFAULT nextval('public.job_card_output_output_id_seq'::regclass);


--
-- Name: job_card_partial_dispatch dispatch_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_partial_dispatch ALTER COLUMN dispatch_id SET DEFAULT nextval('public.job_card_partial_dispatch_dispatch_id_seq'::regclass);


--
-- Name: job_card_pm_indent pm_indent_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_pm_indent ALTER COLUMN pm_indent_id SET DEFAULT nextval('public.job_card_pm_indent_pm_indent_id_seq'::regclass);


--
-- Name: job_card_remarks remark_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_remarks ALTER COLUMN remark_id SET DEFAULT nextval('public.job_card_remarks_remark_id_seq'::regclass);


--
-- Name: job_card_rm_indent rm_indent_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_rm_indent ALTER COLUMN rm_indent_id SET DEFAULT nextval('public.job_card_rm_indent_rm_indent_id_seq'::regclass);


--
-- Name: job_card_shift_log log_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_shift_log ALTER COLUMN log_id SET DEFAULT nextval('public.job_card_shift_log_log_id_seq'::regclass);


--
-- Name: job_card_sign_off sign_off_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_sign_off ALTER COLUMN sign_off_id SET DEFAULT nextval('public.job_card_sign_off_sign_off_id_seq'::regclass);


--
-- Name: job_card_weight_check check_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_weight_check ALTER COLUMN check_id SET DEFAULT nextval('public.job_card_weight_check_check_id_seq'::regclass);


--
-- Name: legacy_import_log import_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.legacy_import_log ALTER COLUMN import_id SET DEFAULT nextval('public.legacy_import_log_import_id_seq'::regclass);


--
-- Name: log_edit log_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.log_edit ALTER COLUMN log_id SET DEFAULT nextval('public.log_edit_log_id_seq'::regclass);


--
-- Name: lot_block id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lot_block ALTER COLUMN id SET DEFAULT nextval('public.lot_block_id_seq'::regclass);


--
-- Name: machine machine_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.machine ALTER COLUMN machine_id SET DEFAULT nextval('public.machine_machine_id_seq'::regclass);


--
-- Name: machine_capacity capacity_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.machine_capacity ALTER COLUMN capacity_id SET DEFAULT nextval('public.machine_capacity_capacity_id_seq'::regclass);


--
-- Name: material_document id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.material_document ALTER COLUMN id SET DEFAULT nextval('public.material_document_id_seq'::regclass);


--
-- Name: material_document_line id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.material_document_line ALTER COLUMN id SET DEFAULT nextval('public.material_document_line_id_seq'::regclass);


--
-- Name: ncr_event_log event_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ncr_event_log ALTER COLUMN event_id SET DEFAULT nextval('public.ncr_event_log_event_id_seq'::regclass);


--
-- Name: ncr_parameter_detail detail_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ncr_parameter_detail ALTER COLUMN detail_id SET DEFAULT nextval('public.ncr_parameter_detail_detail_id_seq'::regclass);


--
-- Name: ncr_supplier_action action_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ncr_supplier_action ALTER COLUMN action_id SET DEFAULT nextval('public.ncr_supplier_action_action_id_seq'::regclass);


--
-- Name: npd_authorized_users id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_authorized_users ALTER COLUMN id SET DEFAULT nextval('public.npd_authorized_users_id_seq'::regclass);


--
-- Name: npd_dev_job_card_lines id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_dev_job_card_lines ALTER COLUMN id SET DEFAULT nextval('public.npd_dev_job_card_lines_id_seq'::regclass);


--
-- Name: npd_draft_bom_lines id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_draft_bom_lines ALTER COLUMN id SET DEFAULT nextval('public.npd_draft_bom_lines_id_seq'::regclass);


--
-- Name: npd_draft_boms id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_draft_boms ALTER COLUMN id SET DEFAULT nextval('public.npd_draft_boms_id_seq'::regclass);


--
-- Name: off_grade_inventory id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.off_grade_inventory ALTER COLUMN id SET DEFAULT nextval('public.off_grade_inventory_id_seq'::regclass);


--
-- Name: offgrade_consumption consumption_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offgrade_consumption ALTER COLUMN consumption_id SET DEFAULT nextval('public.offgrade_consumption_consumption_id_seq'::regclass);


--
-- Name: offgrade_inventory offgrade_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offgrade_inventory ALTER COLUMN offgrade_id SET DEFAULT nextval('public.offgrade_inventory_offgrade_id_seq'::regclass);


--
-- Name: offgrade_reuse_rule rule_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offgrade_reuse_rule ALTER COLUMN rule_id SET DEFAULT nextval('public.offgrade_reuse_rule_rule_id_seq'::regclass);


--
-- Name: po_event_log event_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.po_event_log ALTER COLUMN event_id SET DEFAULT nextval('public.po_event_log_event_id_seq'::regclass);


--
-- Name: process_loss loss_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.process_loss ALTER COLUMN loss_id SET DEFAULT nextval('public.process_loss_loss_id_seq'::regclass);


--
-- Name: production_indent id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.production_indent ALTER COLUMN id SET DEFAULT nextval('public.production_indent_id_seq'::regclass);


--
-- Name: production_order prod_order_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.production_order ALTER COLUMN prod_order_id SET DEFAULT nextval('public.production_order_prod_order_id_seq'::regclass);


--
-- Name: production_plan plan_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.production_plan ALTER COLUMN plan_id SET DEFAULT nextval('public.production_plan_plan_id_seq'::regclass);


--
-- Name: production_plan_line plan_line_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.production_plan_line ALTER COLUMN plan_line_id SET DEFAULT nextval('public.production_plan_line_plan_line_id_seq'::regclass);


--
-- Name: purchase_indent indent_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_indent ALTER COLUMN indent_id SET DEFAULT nextval('public.purchase_indent_indent_id_seq'::regclass);


--
-- Name: qc_inspection id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qc_inspection ALTER COLUMN id SET DEFAULT nextval('public.qc_inspection_id_seq'::regclass);


--
-- Name: qc_parameter parameter_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qc_parameter ALTER COLUMN parameter_id SET DEFAULT nextval('public.qc_parameter_parameter_id_seq'::regclass);


--
-- Name: quality_inspection inspection_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.quality_inspection ALTER COLUMN inspection_id SET DEFAULT nextval('public.quality_inspection_inspection_id_seq'::regclass);


--
-- Name: receipt_document document_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.receipt_document ALTER COLUMN document_id SET DEFAULT nextval('public.receipt_document_document_id_seq'::regclass);


--
-- Name: reconciliation_failures failure_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reconciliation_failures ALTER COLUMN failure_id SET DEFAULT nextval('public.reconciliation_failures_failure_id_seq'::regclass);


--
-- Name: rm_issue_form id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rm_issue_form ALTER COLUMN id SET DEFAULT nextval('public.rm_issue_form_id_seq'::regclass);


--
-- Name: rm_issue_form_lines id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rm_issue_form_lines ALTER COLUMN id SET DEFAULT nextval('public.rm_issue_form_lines_id_seq'::regclass);


--
-- Name: rtv_disposition id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rtv_disposition ALTER COLUMN id SET DEFAULT nextval('public.rtv_disposition_id_seq'::regclass);


--
-- Name: sample_approval_role_map id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_approval_role_map ALTER COLUMN id SET DEFAULT nextval('public.sample_approval_role_map_id_seq'::regclass);


--
-- Name: sample_approvals id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_approvals ALTER COLUMN id SET DEFAULT nextval('public.sample_approvals_id_seq'::regclass);


--
-- Name: sample_audit_log id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_audit_log ALTER COLUMN id SET DEFAULT nextval('public.sample_audit_log_id_seq'::regclass);


--
-- Name: sample_consumption_variance id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_consumption_variance ALTER COLUMN id SET DEFAULT nextval('public.sample_consumption_variance_id_seq'::regclass);


--
-- Name: sample_notification_map id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_notification_map ALTER COLUMN id SET DEFAULT nextval('public.sample_notification_map_id_seq'::regclass);


--
-- Name: sample_requisition_articles id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_requisition_articles ALTER COLUMN id SET DEFAULT nextval('public.sample_requisition_articles_id_seq'::regclass);


--
-- Name: sample_requisitions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_requisitions ALTER COLUMN id SET DEFAULT nextval('public.sample_requisitions_id_seq'::regclass);


--
-- Name: so_fulfillment fulfillment_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.so_fulfillment ALTER COLUMN fulfillment_id SET DEFAULT nextval('public.so_fulfillment_fulfillment_id_seq'::regclass);


--
-- Name: so_gst_reconciliation recon_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.so_gst_reconciliation ALTER COLUMN recon_id SET DEFAULT nextval('public.so_gst_reconciliation_recon_id_seq'::regclass);


--
-- Name: so_header so_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.so_header ALTER COLUMN so_id SET DEFAULT nextval('public.so_header_so_id_seq'::regclass);


--
-- Name: so_revision_log revision_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.so_revision_log ALTER COLUMN revision_id SET DEFAULT nextval('public.so_revision_log_revision_id_seq'::regclass);


--
-- Name: store_alert alert_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.store_alert ALTER COLUMN alert_id SET DEFAULT nextval('public.store_alert_alert_id_seq'::regclass);


--
-- Name: store_allocation allocation_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.store_allocation ALTER COLUMN allocation_id SET DEFAULT nextval('public.store_allocation_allocation_id_seq'::regclass);


--
-- Name: webhook_delivery id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_delivery ALTER COLUMN id SET DEFAULT nextval('public.webhook_delivery_id_seq'::regclass);


--
-- Name: webhook_endpoint id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_endpoint ALTER COLUMN id SET DEFAULT nextval('public.webhook_endpoint_id_seq'::regclass);


--
-- Name: webhook_subscription id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_subscription ALTER COLUMN id SET DEFAULT nextval('public.webhook_subscription_id_seq'::regclass);


--
-- Name: write_off_ledger id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.write_off_ledger ALTER COLUMN id SET DEFAULT nextval('public.write_off_ledger_id_seq'::regclass);


--
-- Name: yield_summary yield_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.yield_summary ALTER COLUMN yield_id SET DEFAULT nextval('public.yield_summary_yield_id_seq'::regclass);


--
-- Name: ai_recommendation ai_recommendation_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_recommendation
    ADD CONSTRAINT ai_recommendation_pkey PRIMARY KEY (recommendation_id);


--
-- Name: all_sku all_sku_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.all_sku
    ADD CONSTRAINT all_sku_pkey PRIMARY KEY (sku_id);


--
-- Name: amendment_log amendment_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.amendment_log
    ADD CONSTRAINT amendment_log_pkey PRIMARY KEY (id);


--
-- Name: auth_password_reset_otp auth_password_reset_otp_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_password_reset_otp
    ADD CONSTRAINT auth_password_reset_otp_pkey PRIMARY KEY (user_id);


--
-- Name: auth_permission auth_permission_module_sub_module_sub_sub_module_action_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_permission
    ADD CONSTRAINT auth_permission_module_sub_module_sub_sub_module_action_key UNIQUE (module, sub_module, sub_sub_module, action);


--
-- Name: auth_permission auth_permission_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_permission
    ADD CONSTRAINT auth_permission_pkey PRIMARY KEY (permission_id);


--
-- Name: auth_refresh_token auth_refresh_token_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_refresh_token
    ADD CONSTRAINT auth_refresh_token_pkey PRIMARY KEY (jti);


--
-- Name: auth_role_permission auth_role_permission_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_role_permission
    ADD CONSTRAINT auth_role_permission_pkey PRIMARY KEY (role_id, permission_id);


--
-- Name: auth_role auth_role_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_role
    ADD CONSTRAINT auth_role_pkey PRIMARY KEY (role_id);


--
-- Name: auth_role auth_role_role_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_role
    ADD CONSTRAINT auth_role_role_name_key UNIQUE (role_name);


--
-- Name: auth_session auth_session_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_session
    ADD CONSTRAINT auth_session_pkey PRIMARY KEY (session_id);


--
-- Name: auth_session auth_session_token_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_session
    ADD CONSTRAINT auth_session_token_key UNIQUE (token);


--
-- Name: auth_user auth_user_phone_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_user
    ADD CONSTRAINT auth_user_phone_key UNIQUE (phone);


--
-- Name: auth_user auth_user_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_user
    ADD CONSTRAINT auth_user_pkey PRIMARY KEY (user_id);


--
-- Name: auth_user_role auth_user_role_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_user_role
    ADD CONSTRAINT auth_user_role_pkey PRIMARY KEY (user_id, role_id);


--
-- Name: batch_block_history batch_block_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.batch_block_history
    ADD CONSTRAINT batch_block_history_pkey PRIMARY KEY (id);


--
-- Name: batch_rejection_log batch_rejection_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.batch_rejection_log
    ADD CONSTRAINT batch_rejection_log_pkey PRIMARY KEY (log_id);


--
-- Name: bom_amendment_request_v2 bom_amendment_request_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bom_amendment_request_v2
    ADD CONSTRAINT bom_amendment_request_v2_pkey PRIMARY KEY (request_id);


--
-- Name: bom_header bom_header_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bom_header
    ADD CONSTRAINT bom_header_pkey PRIMARY KEY (bom_id);


--
-- Name: bom_line bom_line_bom_id_line_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bom_line
    ADD CONSTRAINT bom_line_bom_id_line_number_key UNIQUE (bom_id, line_number);


--
-- Name: bom_line bom_line_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bom_line
    ADD CONSTRAINT bom_line_pkey PRIMARY KEY (bom_line_id);


--
-- Name: bom_process_route bom_process_route_bom_id_step_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bom_process_route
    ADD CONSTRAINT bom_process_route_bom_id_step_number_key UNIQUE (bom_id, step_number);


--
-- Name: bom_process_route bom_process_route_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bom_process_route
    ADD CONSTRAINT bom_process_route_pkey PRIMARY KEY (route_id);


--
-- Name: cascade_events cascade_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cascade_events
    ADD CONSTRAINT cascade_events_pkey PRIMARY KEY (event_id);


--
-- Name: coa_document coa_document_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.coa_document
    ADD CONSTRAINT coa_document_pkey PRIMARY KEY (coa_id);


--
-- Name: day_end_balance_scan day_end_balance_scan_floor_location_scan_date_entity_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.day_end_balance_scan
    ADD CONSTRAINT day_end_balance_scan_floor_location_scan_date_entity_key UNIQUE (floor_location, scan_date, entity);


--
-- Name: day_end_balance_scan_line day_end_balance_scan_line_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.day_end_balance_scan_line
    ADD CONSTRAINT day_end_balance_scan_line_pkey PRIMARY KEY (scan_line_id);


--
-- Name: day_end_balance_scan day_end_balance_scan_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.day_end_balance_scan
    ADD CONSTRAINT day_end_balance_scan_pkey PRIMARY KEY (scan_id);


--
-- Name: discrepancy_report discrepancy_report_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.discrepancy_report
    ADD CONSTRAINT discrepancy_report_pkey PRIMARY KEY (discrepancy_id);


--
-- Name: fifo_skip_log fifo_skip_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fifo_skip_log
    ADD CONSTRAINT fifo_skip_log_pkey PRIMARY KEY (id);


--
-- Name: floor_inventory floor_inventory_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.floor_inventory
    ADD CONSTRAINT floor_inventory_pkey PRIMARY KEY (inventory_id);


--
-- Name: floor_inventory floor_inventory_sku_name_floor_location_lot_number_entity_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.floor_inventory
    ADD CONSTRAINT floor_inventory_sku_name_floor_location_lot_number_entity_key UNIQUE (sku_name, floor_location, lot_number, entity);


--
-- Name: floor_movement floor_movement_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.floor_movement
    ADD CONSTRAINT floor_movement_pkey PRIMARY KEY (movement_id);


--
-- Name: fulfillment_bom_override fulfillment_bom_override_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fulfillment_bom_override
    ADD CONSTRAINT fulfillment_bom_override_pkey PRIMARY KEY (override_id);


--
-- Name: fulfillment_bom_override_v2 fulfillment_bom_override_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fulfillment_bom_override_v2
    ADD CONSTRAINT fulfillment_bom_override_v2_pkey PRIMARY KEY (override_id);


--
-- Name: fulfillment_floor_stock fulfillment_floor_stock_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fulfillment_floor_stock
    ADD CONSTRAINT fulfillment_floor_stock_pkey PRIMARY KEY (floor_stock_id);


--
-- Name: fulfillment_floor_stock_v2 fulfillment_floor_stock_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fulfillment_floor_stock_v2
    ADD CONSTRAINT fulfillment_floor_stock_v2_pkey PRIMARY KEY (floor_stock_id);


--
-- Name: gate_pass_sample_details gate_pass_sample_details_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gate_pass_sample_details
    ADD CONSTRAINT gate_pass_sample_details_pkey PRIMARY KEY (gate_pass_id);


--
-- Name: gate_passes gate_passes_gate_pass_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gate_passes
    ADD CONSTRAINT gate_passes_gate_pass_number_key UNIQUE (gate_pass_number);


--
-- Name: gate_passes gate_passes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gate_passes
    ADD CONSTRAINT gate_passes_pkey PRIMARY KEY (id);


--
-- Name: inter_entity_transfer_line inter_entity_transfer_line_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inter_entity_transfer_line
    ADD CONSTRAINT inter_entity_transfer_line_pkey PRIMARY KEY (id);


--
-- Name: inter_entity_transfer inter_entity_transfer_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inter_entity_transfer
    ADD CONSTRAINT inter_entity_transfer_pkey PRIMARY KEY (id);


--
-- Name: inter_entity_transfer inter_entity_transfer_transfer_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inter_entity_transfer
    ADD CONSTRAINT inter_entity_transfer_transfer_id_key UNIQUE (transfer_id);


--
-- Name: internal_issue_note internal_issue_note_note_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.internal_issue_note
    ADD CONSTRAINT internal_issue_note_note_number_key UNIQUE (note_number);


--
-- Name: internal_issue_note internal_issue_note_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.internal_issue_note
    ADD CONSTRAINT internal_issue_note_pkey PRIMARY KEY (note_id);


--
-- Name: internal_order internal_order_internal_order_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.internal_order
    ADD CONSTRAINT internal_order_internal_order_id_key UNIQUE (internal_order_id);


--
-- Name: internal_order internal_order_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.internal_order
    ADD CONSTRAINT internal_order_pkey PRIMARY KEY (id);


--
-- Name: interunit_transfer_boxes interunit_transfer_boxes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.interunit_transfer_boxes
    ADD CONSTRAINT interunit_transfer_boxes_pkey PRIMARY KEY (id);


--
-- Name: interunit_transfer_in_boxes interunit_transfer_in_boxes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.interunit_transfer_in_boxes
    ADD CONSTRAINT interunit_transfer_in_boxes_pkey PRIMARY KEY (id);


--
-- Name: interunit_transfer_in_header interunit_transfer_in_header_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.interunit_transfer_in_header
    ADD CONSTRAINT interunit_transfer_in_header_pkey PRIMARY KEY (id);


--
-- Name: interunit_transfers_header interunit_transfers_header_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.interunit_transfers_header
    ADD CONSTRAINT interunit_transfers_header_pkey PRIMARY KEY (id);


--
-- Name: interunit_transfers_lines interunit_transfers_lines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.interunit_transfers_lines
    ADD CONSTRAINT interunit_transfers_lines_pkey PRIMARY KEY (id);


--
-- Name: inventory_batch inventory_batch_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inventory_batch
    ADD CONSTRAINT inventory_batch_pkey PRIMARY KEY (batch_id);


--
-- Name: inventory_event_log inventory_event_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inventory_event_log
    ADD CONSTRAINT inventory_event_log_pkey PRIMARY KEY (event_id);


--
-- Name: issue_note issue_note_issue_note_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.issue_note
    ADD CONSTRAINT issue_note_issue_note_id_key UNIQUE (issue_note_id);


--
-- Name: issue_note_line issue_note_line_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.issue_note_line
    ADD CONSTRAINT issue_note_line_pkey PRIMARY KEY (id);


--
-- Name: issue_note issue_note_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.issue_note
    ADD CONSTRAINT issue_note_pkey PRIMARY KEY (id);


--
-- Name: jc_material_exception_v2 jc_material_exception_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jc_material_exception_v2
    ADD CONSTRAINT jc_material_exception_v2_pkey PRIMARY KEY (exception_id);


--
-- Name: job_card_accounting_v2 job_card_accounting_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_accounting_v2
    ADD CONSTRAINT job_card_accounting_v2_pkey PRIMARY KEY (accounting_id);


--
-- Name: job_card_additive_consumption_v2 job_card_additive_consumption_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_additive_consumption_v2
    ADD CONSTRAINT job_card_additive_consumption_v2_pkey PRIMARY KEY (additive_id);


--
-- Name: job_card_balance_material job_card_balance_material_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_balance_material
    ADD CONSTRAINT job_card_balance_material_pkey PRIMARY KEY (balance_id);


--
-- Name: job_card_balance_material_v2 job_card_balance_material_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_balance_material_v2
    ADD CONSTRAINT job_card_balance_material_v2_pkey PRIMARY KEY (balance_id);


--
-- Name: job_card_byproducts_v2 job_card_byproducts_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_byproducts_v2
    ADD CONSTRAINT job_card_byproducts_v2_pkey PRIMARY KEY (byproduct_id);


--
-- Name: job_card_consumption_variance_v2 job_card_consumption_variance_job_card_id_material_sku_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_consumption_variance_v2
    ADD CONSTRAINT job_card_consumption_variance_job_card_id_material_sku_name_key UNIQUE (job_card_id, material_sku_name);


--
-- Name: job_card_consumption_variance_v2 job_card_consumption_variance_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_consumption_variance_v2
    ADD CONSTRAINT job_card_consumption_variance_v2_pkey PRIMARY KEY (variance_id);


--
-- Name: job_card_environment job_card_environment_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_environment
    ADD CONSTRAINT job_card_environment_pkey PRIMARY KEY (env_id);


--
-- Name: job_card_environment_v2 job_card_environment_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_environment_v2
    ADD CONSTRAINT job_card_environment_v2_pkey PRIMARY KEY (env_id);


--
-- Name: job_card_loss_reconciliation job_card_loss_reconciliation_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_loss_reconciliation
    ADD CONSTRAINT job_card_loss_reconciliation_pkey PRIMARY KEY (recon_id);


--
-- Name: job_card_loss_reconciliation_v2 job_card_loss_reconciliation_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_loss_reconciliation_v2
    ADD CONSTRAINT job_card_loss_reconciliation_v2_pkey PRIMARY KEY (recon_id);


--
-- Name: job_card_material_consumption job_card_material_consumption_job_card_id_material_sku_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_material_consumption
    ADD CONSTRAINT job_card_material_consumption_job_card_id_material_sku_name_key UNIQUE (job_card_id, material_sku_name, item_type);


--
-- Name: job_card_material_consumption job_card_material_consumption_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_material_consumption
    ADD CONSTRAINT job_card_material_consumption_pkey PRIMARY KEY (consumption_id);


--
-- Name: job_card_material_consumption_v2 job_card_material_consumption_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_material_consumption_v2
    ADD CONSTRAINT job_card_material_consumption_v2_pkey PRIMARY KEY (consumption_id);


--
-- Name: job_card_metal_detection job_card_metal_detection_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_metal_detection
    ADD CONSTRAINT job_card_metal_detection_pkey PRIMARY KEY (detection_id);


--
-- Name: job_card_metal_detection_v2 job_card_metal_detection_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_metal_detection_v2
    ADD CONSTRAINT job_card_metal_detection_v2_pkey PRIMARY KEY (detection_id);


--
-- Name: job_card_output job_card_output_job_card_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_output
    ADD CONSTRAINT job_card_output_job_card_id_key UNIQUE (job_card_id);


--
-- Name: job_card_output job_card_output_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_output
    ADD CONSTRAINT job_card_output_pkey PRIMARY KEY (output_id);


--
-- Name: job_card_output_v2 job_card_output_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_output_v2
    ADD CONSTRAINT job_card_output_v2_pkey PRIMARY KEY (output_id);


--
-- Name: job_card_partial_dispatch job_card_partial_dispatch_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_partial_dispatch
    ADD CONSTRAINT job_card_partial_dispatch_pkey PRIMARY KEY (dispatch_id);


--
-- Name: job_card_partial_dispatch_v2 job_card_partial_dispatch_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_partial_dispatch_v2
    ADD CONSTRAINT job_card_partial_dispatch_v2_pkey PRIMARY KEY (dispatch_id);


--
-- Name: job_card_phase_v2 job_card_phase_v2_job_card_id_phase_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_phase_v2
    ADD CONSTRAINT job_card_phase_v2_job_card_id_phase_number_key UNIQUE (job_card_id, phase_number);


--
-- Name: job_card_phase_v2 job_card_phase_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_phase_v2
    ADD CONSTRAINT job_card_phase_v2_pkey PRIMARY KEY (phase_id);


--
-- Name: job_card_pm_indent job_card_pm_indent_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_pm_indent
    ADD CONSTRAINT job_card_pm_indent_pkey PRIMARY KEY (pm_indent_id);


--
-- Name: job_card_pm_indent_v2 job_card_pm_indent_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_pm_indent_v2
    ADD CONSTRAINT job_card_pm_indent_v2_pkey PRIMARY KEY (pm_indent_id);


--
-- Name: job_card_qc_v2 job_card_qc_v2_job_card_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_qc_v2
    ADD CONSTRAINT job_card_qc_v2_job_card_id_key UNIQUE (job_card_id);


--
-- Name: job_card_qc_v2 job_card_qc_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_qc_v2
    ADD CONSTRAINT job_card_qc_v2_pkey PRIMARY KEY (qc_id);


--
-- Name: job_card_remarks job_card_remarks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_remarks
    ADD CONSTRAINT job_card_remarks_pkey PRIMARY KEY (remark_id);


--
-- Name: job_card_remarks_v2 job_card_remarks_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_remarks_v2
    ADD CONSTRAINT job_card_remarks_v2_pkey PRIMARY KEY (remark_id);


--
-- Name: job_card_rm_indent job_card_rm_indent_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_rm_indent
    ADD CONSTRAINT job_card_rm_indent_pkey PRIMARY KEY (rm_indent_id);


--
-- Name: job_card_rm_indent_v2 job_card_rm_indent_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_rm_indent_v2
    ADD CONSTRAINT job_card_rm_indent_v2_pkey PRIMARY KEY (rm_indent_id);


--
-- Name: job_card_shift_log job_card_shift_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_shift_log
    ADD CONSTRAINT job_card_shift_log_pkey PRIMARY KEY (log_id);


--
-- Name: job_card_shift_log_v2 job_card_shift_log_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_shift_log_v2
    ADD CONSTRAINT job_card_shift_log_v2_pkey PRIMARY KEY (log_id);


--
-- Name: job_card_sign_off job_card_sign_off_job_card_id_sign_off_type_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_sign_off
    ADD CONSTRAINT job_card_sign_off_job_card_id_sign_off_type_key UNIQUE (job_card_id, sign_off_type);


--
-- Name: job_card_sign_off job_card_sign_off_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_sign_off
    ADD CONSTRAINT job_card_sign_off_pkey PRIMARY KEY (sign_off_id);


--
-- Name: job_card_sign_off_v2 job_card_sign_off_v2_job_card_id_role_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_sign_off_v2
    ADD CONSTRAINT job_card_sign_off_v2_job_card_id_role_key UNIQUE (job_card_id, role);


--
-- Name: job_card_sign_off_v2 job_card_sign_off_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_sign_off_v2
    ADD CONSTRAINT job_card_sign_off_v2_pkey PRIMARY KEY (sign_off_id);


--
-- Name: job_card_v2 job_card_v2_job_card_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_v2
    ADD CONSTRAINT job_card_v2_job_card_number_key UNIQUE (job_card_number);


--
-- Name: job_card_v2 job_card_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_v2
    ADD CONSTRAINT job_card_v2_pkey PRIMARY KEY (job_card_id);


--
-- Name: job_card_weight_check job_card_weight_check_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_weight_check
    ADD CONSTRAINT job_card_weight_check_pkey PRIMARY KEY (check_id);


--
-- Name: job_card_weight_check_v2 job_card_weight_check_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_weight_check_v2
    ADD CONSTRAINT job_card_weight_check_v2_pkey PRIMARY KEY (check_id);


--
-- Name: legacy_import_log legacy_import_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.legacy_import_log
    ADD CONSTRAINT legacy_import_log_pkey PRIMARY KEY (import_id);


--
-- Name: log_edit log_edit_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.log_edit
    ADD CONSTRAINT log_edit_pkey PRIMARY KEY (log_id);


--
-- Name: lot_block lot_block_block_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lot_block
    ADD CONSTRAINT lot_block_block_id_key UNIQUE (block_id);


--
-- Name: lot_block lot_block_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lot_block
    ADD CONSTRAINT lot_block_pkey PRIMARY KEY (id);


--
-- Name: machine_capacity machine_capacity_machine_id_stage_item_group_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.machine_capacity
    ADD CONSTRAINT machine_capacity_machine_id_stage_item_group_key UNIQUE (machine_id, stage, item_group);


--
-- Name: machine_capacity machine_capacity_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.machine_capacity
    ADD CONSTRAINT machine_capacity_pkey PRIMARY KEY (capacity_id);


--
-- Name: machine machine_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.machine
    ADD CONSTRAINT machine_pkey PRIMARY KEY (machine_id);


--
-- Name: material_document_line material_document_line_mat_doc_id_line_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.material_document_line
    ADD CONSTRAINT material_document_line_mat_doc_id_line_number_key UNIQUE (mat_doc_id, line_number);


--
-- Name: material_document_line material_document_line_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.material_document_line
    ADD CONSTRAINT material_document_line_pkey PRIMARY KEY (id);


--
-- Name: material_document material_document_mat_doc_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.material_document
    ADD CONSTRAINT material_document_mat_doc_id_key UNIQUE (mat_doc_id);


--
-- Name: material_document material_document_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.material_document
    ADD CONSTRAINT material_document_pkey PRIMARY KEY (id);


--
-- Name: movement_type_ref movement_type_ref_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.movement_type_ref
    ADD CONSTRAINT movement_type_ref_pkey PRIMARY KEY (movement_type);


--
-- Name: ncr_supplier_action ncr_capa_round_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ncr_supplier_action
    ADD CONSTRAINT ncr_capa_round_unique UNIQUE (ncr_no, round_no);


--
-- Name: ncr_event_log ncr_event_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ncr_event_log
    ADD CONSTRAINT ncr_event_log_pkey PRIMARY KEY (event_id);


--
-- Name: ncr_parameter_detail ncr_parameter_detail_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ncr_parameter_detail
    ADD CONSTRAINT ncr_parameter_detail_pkey PRIMARY KEY (detail_id);


--
-- Name: ncr_record ncr_record_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ncr_record
    ADD CONSTRAINT ncr_record_pkey PRIMARY KEY (ncr_no);


--
-- Name: ncr_supplier_action ncr_supplier_action_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ncr_supplier_action
    ADD CONSTRAINT ncr_supplier_action_pkey PRIMARY KEY (action_id);


--
-- Name: npd_authorized_users npd_authorized_users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_authorized_users
    ADD CONSTRAINT npd_authorized_users_pkey PRIMARY KEY (id);


--
-- Name: npd_authorized_users npd_authorized_users_user_id_capability_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_authorized_users
    ADD CONSTRAINT npd_authorized_users_user_id_capability_key UNIQUE (user_id, capability);


--
-- Name: npd_dev_job_card_lines npd_dev_job_card_lines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_dev_job_card_lines
    ADD CONSTRAINT npd_dev_job_card_lines_pkey PRIMARY KEY (id);


--
-- Name: npd_dev_job_card_phases npd_dev_job_card_phases_dev_jc_id_phase_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_dev_job_card_phases
    ADD CONSTRAINT npd_dev_job_card_phases_dev_jc_id_phase_number_key UNIQUE (dev_jc_id, phase_number);


--
-- Name: npd_dev_job_card_phases npd_dev_job_card_phases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_dev_job_card_phases
    ADD CONSTRAINT npd_dev_job_card_phases_pkey PRIMARY KEY (phase_id);


--
-- Name: npd_dev_job_cards npd_dev_job_cards_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_dev_job_cards
    ADD CONSTRAINT npd_dev_job_cards_pkey PRIMARY KEY (id);


--
-- Name: npd_dev_promote_approval npd_dev_promote_approval_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_dev_promote_approval
    ADD CONSTRAINT npd_dev_promote_approval_pkey PRIMARY KEY (id);


--
-- Name: npd_dev_promote_request npd_dev_promote_request_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_dev_promote_request
    ADD CONSTRAINT npd_dev_promote_request_pkey PRIMARY KEY (id);


--
-- Name: npd_draft_bom_lines npd_draft_bom_lines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_draft_bom_lines
    ADD CONSTRAINT npd_draft_bom_lines_pkey PRIMARY KEY (id);


--
-- Name: npd_draft_boms npd_draft_boms_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_draft_boms
    ADD CONSTRAINT npd_draft_boms_pkey PRIMARY KEY (id);


--
-- Name: off_grade_inventory off_grade_inventory_offgrade_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.off_grade_inventory
    ADD CONSTRAINT off_grade_inventory_offgrade_id_key UNIQUE (offgrade_id);


--
-- Name: off_grade_inventory off_grade_inventory_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.off_grade_inventory
    ADD CONSTRAINT off_grade_inventory_pkey PRIMARY KEY (id);


--
-- Name: offgrade_consumption offgrade_consumption_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offgrade_consumption
    ADD CONSTRAINT offgrade_consumption_pkey PRIMARY KEY (consumption_id);


--
-- Name: offgrade_inventory offgrade_inventory_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offgrade_inventory
    ADD CONSTRAINT offgrade_inventory_pkey PRIMARY KEY (offgrade_id);


--
-- Name: offgrade_reuse_rule offgrade_reuse_rule_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offgrade_reuse_rule
    ADD CONSTRAINT offgrade_reuse_rule_pkey PRIMARY KEY (rule_id);


--
-- Name: offgrade_reuse_rule offgrade_reuse_rule_source_item_group_target_item_group_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offgrade_reuse_rule
    ADD CONSTRAINT offgrade_reuse_rule_source_item_group_target_item_group_key UNIQUE (source_item_group, target_item_group);


--
-- Name: pending_transfer_stock pending_transfer_stock_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pending_transfer_stock
    ADD CONSTRAINT pending_transfer_stock_pkey PRIMARY KEY (id);


--
-- Name: po_box po_box_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.po_box
    ADD CONSTRAINT po_box_pkey PRIMARY KEY (box_id);


--
-- Name: po_event_log po_event_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.po_event_log
    ADD CONSTRAINT po_event_log_pkey PRIMARY KEY (event_id);


--
-- Name: po_header po_header_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.po_header
    ADD CONSTRAINT po_header_pkey PRIMARY KEY (transaction_no);


--
-- Name: po_line po_line_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.po_line
    ADD CONSTRAINT po_line_pkey PRIMARY KEY (transaction_no, line_number);


--
-- Name: po_section po_section_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.po_section
    ADD CONSTRAINT po_section_pkey PRIMARY KEY (transaction_no, line_number, section_number);


--
-- Name: process_loss process_loss_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.process_loss
    ADD CONSTRAINT process_loss_pkey PRIMARY KEY (loss_id);


--
-- Name: production_indent production_indent_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.production_indent
    ADD CONSTRAINT production_indent_pkey PRIMARY KEY (id);


--
-- Name: production_indent production_indent_prod_indent_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.production_indent
    ADD CONSTRAINT production_indent_prod_indent_id_key UNIQUE (prod_indent_id);


--
-- Name: production_order production_order_batch_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.production_order
    ADD CONSTRAINT production_order_batch_number_key UNIQUE (batch_number);


--
-- Name: production_order production_order_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.production_order
    ADD CONSTRAINT production_order_pkey PRIMARY KEY (prod_order_id);


--
-- Name: production_order production_order_prod_order_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.production_order
    ADD CONSTRAINT production_order_prod_order_number_key UNIQUE (prod_order_number);


--
-- Name: production_plan_line production_plan_line_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.production_plan_line
    ADD CONSTRAINT production_plan_line_pkey PRIMARY KEY (plan_line_id);


--
-- Name: production_plan_line_v2 production_plan_line_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.production_plan_line_v2
    ADD CONSTRAINT production_plan_line_v2_pkey PRIMARY KEY (plan_line_id);


--
-- Name: production_plan production_plan_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.production_plan
    ADD CONSTRAINT production_plan_pkey PRIMARY KEY (plan_id);


--
-- Name: production_plan_step_v2 production_plan_step_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.production_plan_step_v2
    ADD CONSTRAINT production_plan_step_v2_pkey PRIMARY KEY (step_id);


--
-- Name: production_plan_v2 production_plan_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.production_plan_v2
    ADD CONSTRAINT production_plan_v2_pkey PRIMARY KEY (plan_id);


--
-- Name: purchase_indent purchase_indent_indent_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_indent
    ADD CONSTRAINT purchase_indent_indent_number_key UNIQUE (indent_number);


--
-- Name: purchase_indent purchase_indent_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_indent
    ADD CONSTRAINT purchase_indent_pkey PRIMARY KEY (indent_id);


--
-- Name: qc_inspection qc_inspection_inspection_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qc_inspection
    ADD CONSTRAINT qc_inspection_inspection_id_key UNIQUE (inspection_id);


--
-- Name: qc_inspection qc_inspection_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qc_inspection
    ADD CONSTRAINT qc_inspection_pkey PRIMARY KEY (id);


--
-- Name: qc_intimation qc_intimation_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qc_intimation
    ADD CONSTRAINT qc_intimation_pkey PRIMARY KEY (qc_intimation_id);


--
-- Name: qc_inward_inspection_audit qc_inward_inspection_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qc_inward_inspection_audit
    ADD CONSTRAINT qc_inward_inspection_audit_pkey PRIMARY KEY (audit_id);


--
-- Name: qc_inward_inspection qc_inward_inspection_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qc_inward_inspection
    ADD CONSTRAINT qc_inward_inspection_pkey PRIMARY KEY (inspection_id);


--
-- Name: qc_inward_reading qc_inward_reading_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qc_inward_reading
    ADD CONSTRAINT qc_inward_reading_pkey PRIMARY KEY (reading_id);


--
-- Name: qc_notification_log_v2 qc_notification_log_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qc_notification_log_v2
    ADD CONSTRAINT qc_notification_log_v2_pkey PRIMARY KEY (notification_id);


--
-- Name: qc_parameter qc_parameter_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qc_parameter
    ADD CONSTRAINT qc_parameter_name_key UNIQUE (name);


--
-- Name: qc_parameter qc_parameter_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qc_parameter
    ADD CONSTRAINT qc_parameter_pkey PRIMARY KEY (parameter_id);


--
-- Name: qc_sku_spec qc_sku_spec_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qc_sku_spec
    ADD CONSTRAINT qc_sku_spec_pkey PRIMARY KEY (sku_id, parameter_id);


--
-- Name: quality_inspection quality_inspection_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.quality_inspection
    ADD CONSTRAINT quality_inspection_pkey PRIMARY KEY (inspection_id);


--
-- Name: receipt_document receipt_document_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.receipt_document
    ADD CONSTRAINT receipt_document_pkey PRIMARY KEY (document_id);


--
-- Name: reconciliation_failures reconciliation_failures_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reconciliation_failures
    ADD CONSTRAINT reconciliation_failures_pkey PRIMARY KEY (failure_id);


--
-- Name: rm_issue_form rm_issue_form_form_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rm_issue_form
    ADD CONSTRAINT rm_issue_form_form_number_key UNIQUE (form_number);


--
-- Name: rm_issue_form_lines rm_issue_form_lines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rm_issue_form_lines
    ADD CONSTRAINT rm_issue_form_lines_pkey PRIMARY KEY (id);


--
-- Name: rm_issue_form rm_issue_form_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rm_issue_form
    ADD CONSTRAINT rm_issue_form_pkey PRIMARY KEY (id);


--
-- Name: rtv_disposition rtv_disposition_disposition_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rtv_disposition
    ADD CONSTRAINT rtv_disposition_disposition_id_key UNIQUE (disposition_id);


--
-- Name: rtv_disposition rtv_disposition_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rtv_disposition
    ADD CONSTRAINT rtv_disposition_pkey PRIMARY KEY (id);


--
-- Name: sample_approval_role_map sample_approval_role_map_approval_stage_sample_type_entity__key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_approval_role_map
    ADD CONSTRAINT sample_approval_role_map_approval_stage_sample_type_entity__key UNIQUE (approval_stage, sample_type, entity, required_role);


--
-- Name: sample_approval_role_map sample_approval_role_map_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_approval_role_map
    ADD CONSTRAINT sample_approval_role_map_pkey PRIMARY KEY (id);


--
-- Name: sample_approvals sample_approvals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_approvals
    ADD CONSTRAINT sample_approvals_pkey PRIMARY KEY (id);


--
-- Name: sample_audit_log sample_audit_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_audit_log
    ADD CONSTRAINT sample_audit_log_pkey PRIMARY KEY (id);


--
-- Name: sample_config sample_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_config
    ADD CONSTRAINT sample_config_pkey PRIMARY KEY (config_key);


--
-- Name: sample_consumption_variance sample_consumption_variance_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_consumption_variance
    ADD CONSTRAINT sample_consumption_variance_pkey PRIMARY KEY (id);


--
-- Name: sample_notification_map sample_notification_map_event_sample_type_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_notification_map
    ADD CONSTRAINT sample_notification_map_event_sample_type_key UNIQUE (event, sample_type);


--
-- Name: sample_notification_map sample_notification_map_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_notification_map
    ADD CONSTRAINT sample_notification_map_pkey PRIMARY KEY (id);


--
-- Name: sample_requisition_articles sample_requisition_articles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_requisition_articles
    ADD CONSTRAINT sample_requisition_articles_pkey PRIMARY KEY (id);


--
-- Name: sample_requisitions sample_requisitions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_requisitions
    ADD CONSTRAINT sample_requisitions_pkey PRIMARY KEY (request_id);


--
-- Name: sfg_box sfg_box_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sfg_box
    ADD CONSTRAINT sfg_box_pkey PRIMARY KEY (box_id);


--
-- Name: so_fulfillment so_fulfillment_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.so_fulfillment
    ADD CONSTRAINT so_fulfillment_pkey PRIMARY KEY (fulfillment_id);


--
-- Name: so_fulfillment so_fulfillment_so_line_id_financial_year_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.so_fulfillment
    ADD CONSTRAINT so_fulfillment_so_line_id_financial_year_key UNIQUE (so_line_id, financial_year);


--
-- Name: so_fulfillment_v2 so_fulfillment_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.so_fulfillment_v2
    ADD CONSTRAINT so_fulfillment_v2_pkey PRIMARY KEY (so_fulfillment_id);


--
-- Name: so_fulfillment_v2 so_fulfillment_v2_so_line_id_financial_year_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.so_fulfillment_v2
    ADD CONSTRAINT so_fulfillment_v2_so_line_id_financial_year_key UNIQUE (so_line_id, financial_year);


--
-- Name: so_gst_reconciliation so_gst_reconciliation_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.so_gst_reconciliation
    ADD CONSTRAINT so_gst_reconciliation_pkey PRIMARY KEY (recon_id);


--
-- Name: so_header so_header_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.so_header
    ADD CONSTRAINT so_header_pkey PRIMARY KEY (so_id);


--
-- Name: so_line so_line_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.so_line
    ADD CONSTRAINT so_line_pkey PRIMARY KEY (so_line_id);


--
-- Name: so_line so_line_so_id_line_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.so_line
    ADD CONSTRAINT so_line_so_id_line_number_key UNIQUE (so_id, line_number);


--
-- Name: so_revision_log so_revision_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.so_revision_log
    ADD CONSTRAINT so_revision_log_pkey PRIMARY KEY (revision_id);


--
-- Name: so_revision_log_v2 so_revision_log_v2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.so_revision_log_v2
    ADD CONSTRAINT so_revision_log_v2_pkey PRIMARY KEY (revision_id);


--
-- Name: store_alert store_alert_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.store_alert
    ADD CONSTRAINT store_alert_pkey PRIMARY KEY (alert_id);


--
-- Name: store_allocation store_allocation_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.store_allocation
    ADD CONSTRAINT store_allocation_pkey PRIMARY KEY (allocation_id);


--
-- Name: transfer_request_lines transfer_request_lines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.transfer_request_lines
    ADD CONSTRAINT transfer_request_lines_pkey PRIMARY KEY (id);


--
-- Name: transfer_requests transfer_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.transfer_requests
    ADD CONSTRAINT transfer_requests_pkey PRIMARY KEY (id);


--
-- Name: sample_approvals uq_active_approval; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_approvals
    ADD CONSTRAINT uq_active_approval UNIQUE (requisition_id, approval_stage, sequence_no);


--
-- Name: job_card_loss_reconciliation uq_loss_recon_jc_category; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_loss_reconciliation
    ADD CONSTRAINT uq_loss_recon_jc_category UNIQUE (job_card_id, loss_category);


--
-- Name: production_plan_step_v2 uq_pps_v2_line_order; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.production_plan_step_v2
    ADD CONSTRAINT uq_pps_v2_line_order UNIQUE (plan_line_id, step_order) DEFERRABLE;


--
-- Name: sample_requisitions uq_sample_req_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_requisitions
    ADD CONSTRAINT uq_sample_req_id UNIQUE (id);


--
-- Name: vendor_banking_history vendor_banking_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_banking_history
    ADD CONSTRAINT vendor_banking_history_pkey PRIMARY KEY (history_id);


--
-- Name: vendor_banking vendor_banking_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_banking
    ADD CONSTRAINT vendor_banking_pkey PRIMARY KEY (bank_id);


--
-- Name: vendor_contract_history vendor_contract_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_contract_history
    ADD CONSTRAINT vendor_contract_history_pkey PRIMARY KEY (history_id);


--
-- Name: vendor_contract vendor_contract_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_contract
    ADD CONSTRAINT vendor_contract_pkey PRIMARY KEY (contract_id);


--
-- Name: vendor_document_history vendor_document_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_document_history
    ADD CONSTRAINT vendor_document_history_pkey PRIMARY KEY (history_id);


--
-- Name: vendor_document vendor_document_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_document
    ADD CONSTRAINT vendor_document_pkey PRIMARY KEY (doc_id);


--
-- Name: vendor_extraction_staging vendor_extraction_staging_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_extraction_staging
    ADD CONSTRAINT vendor_extraction_staging_pkey PRIMARY KEY (staging_id);


--
-- Name: vendor_master_history vendor_master_history_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_master_history
    ADD CONSTRAINT vendor_master_history_pkey PRIMARY KEY (history_id);


--
-- Name: vendor_master vendor_master_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_master
    ADD CONSTRAINT vendor_master_pkey PRIMARY KEY (vendor_id);


--
-- Name: wa_pending_action wa_pending_action_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wa_pending_action
    ADD CONSTRAINT wa_pending_action_pkey PRIMARY KEY (wa_phone);


--
-- Name: wa_promote_message wa_promote_message_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wa_promote_message
    ADD CONSTRAINT wa_promote_message_pkey PRIMARY KEY (wamid);


--
-- Name: wa_promote_pending wa_promote_pending_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wa_promote_pending
    ADD CONSTRAINT wa_promote_pending_pkey PRIMARY KEY (wa_phone);


--
-- Name: wa_review_message wa_review_message_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wa_review_message
    ADD CONSTRAINT wa_review_message_pkey PRIMARY KEY (wamid);


--
-- Name: warehouse_sites warehouse_sites_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.warehouse_sites
    ADD CONSTRAINT warehouse_sites_pkey PRIMARY KEY (site_code);


--
-- Name: webhook_delivery webhook_delivery_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_delivery
    ADD CONSTRAINT webhook_delivery_pkey PRIMARY KEY (id);


--
-- Name: webhook_endpoint webhook_endpoint_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_endpoint
    ADD CONSTRAINT webhook_endpoint_pkey PRIMARY KEY (id);


--
-- Name: webhook_subscription webhook_subscription_endpoint_id_event_type_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_subscription
    ADD CONSTRAINT webhook_subscription_endpoint_id_event_type_key UNIQUE (endpoint_id, event_type);


--
-- Name: webhook_subscription webhook_subscription_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_subscription
    ADD CONSTRAINT webhook_subscription_pkey PRIMARY KEY (id);


--
-- Name: write_off_ledger write_off_ledger_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.write_off_ledger
    ADD CONSTRAINT write_off_ledger_pkey PRIMARY KEY (id);


--
-- Name: yield_summary yield_summary_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.yield_summary
    ADD CONSTRAINT yield_summary_pkey PRIMARY KEY (yield_id);


--
-- Name: idx_ai_rec_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ai_rec_entity ON public.ai_recommendation USING btree (entity);


--
-- Name: idx_ai_rec_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ai_rec_status ON public.ai_recommendation USING btree (status);


--
-- Name: idx_ai_rec_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ai_rec_type ON public.ai_recommendation USING btree (recommendation_type);


--
-- Name: idx_alert_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_alert_entity ON public.store_alert USING btree (entity);


--
-- Name: idx_alert_read; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_alert_read ON public.store_alert USING btree (is_read);


--
-- Name: idx_alert_team; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_alert_team ON public.store_alert USING btree (target_team);


--
-- Name: idx_all_sku_particulars; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_all_sku_particulars ON public.all_sku USING btree (particulars);


--
-- Name: idx_aml_field; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_aml_field ON public.amendment_log USING btree (record_id, record_type, field_name);


--
-- Name: idx_aml_record; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_aml_record ON public.amendment_log USING btree (record_id, record_type);


--
-- Name: idx_auth_password_reset_otp_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_auth_password_reset_otp_expires_at ON public.auth_password_reset_otp USING btree (expires_at);


--
-- Name: idx_auth_session_token; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_auth_session_token ON public.auth_session USING btree (token);


--
-- Name: idx_auth_session_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_auth_session_user ON public.auth_session USING btree (user_id);


--
-- Name: idx_auth_user_phone; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_auth_user_phone ON public.auth_user USING btree (phone);


--
-- Name: idx_auth_user_role_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_auth_user_role_user ON public.auth_user_role USING btree (user_id);


--
-- Name: idx_balance_mat_bom_line; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_balance_mat_bom_line ON public.job_card_balance_material USING btree (bom_line_id);


--
-- Name: idx_balance_mat_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_balance_mat_jc ON public.job_card_balance_material USING btree (job_card_id);


--
-- Name: idx_balance_scan_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_balance_scan_date ON public.day_end_balance_scan USING btree (scan_date);


--
-- Name: idx_balance_scan_line_scan; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_balance_scan_line_scan ON public.day_end_balance_scan_line USING btree (scan_id);


--
-- Name: idx_balance_scan_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_balance_scan_status ON public.day_end_balance_scan USING btree (status);


--
-- Name: idx_block_hist_batch; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_block_hist_batch ON public.batch_block_history USING btree (batch_id);


--
-- Name: idx_bom_amend_bom; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bom_amend_bom ON public.bom_amendment_request_v2 USING btree (bom_id);


--
-- Name: idx_bom_amend_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bom_amend_jc ON public.bom_amendment_request_v2 USING btree (job_card_id);


--
-- Name: idx_bom_amend_maker; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bom_amend_maker ON public.bom_amendment_request_v2 USING btree (maker_user_id);


--
-- Name: idx_bom_amend_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bom_amend_pending ON public.bom_amendment_request_v2 USING btree (status) WHERE (status = ANY (ARRAY['pending_review'::text, 'pending_final'::text]));


--
-- Name: idx_bom_amend_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bom_amend_status ON public.bom_amendment_request_v2 USING btree (status);


--
-- Name: idx_bom_amend_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bom_amend_type ON public.bom_amendment_request_v2 USING btree (request_type);


--
-- Name: idx_bom_header_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bom_header_active ON public.bom_header USING btree (is_active);


--
-- Name: idx_bom_header_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bom_header_entity ON public.bom_header USING btree (entity);


--
-- Name: idx_bom_header_fg; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bom_header_fg ON public.bom_header USING btree (fg_sku_name);


--
-- Name: idx_bom_line_bom; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bom_line_bom ON public.bom_line USING btree (bom_id);


--
-- Name: idx_bom_override_fulfillment; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bom_override_fulfillment ON public.fulfillment_bom_override USING btree (fulfillment_id);


--
-- Name: idx_bom_override_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_bom_override_unique ON public.fulfillment_bom_override USING btree (fulfillment_id, bom_line_id) WHERE (bom_line_id IS NOT NULL);


--
-- Name: idx_bom_override_v2_fulfillment; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bom_override_v2_fulfillment ON public.fulfillment_bom_override_v2 USING btree (so_fulfillment_id);


--
-- Name: idx_bom_override_v2_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_bom_override_v2_unique ON public.fulfillment_bom_override_v2 USING btree (so_fulfillment_id, bom_line_id) WHERE (bom_line_id IS NOT NULL);


--
-- Name: idx_bom_route_bom; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_bom_route_bom ON public.bom_process_route USING btree (bom_id);


--
-- Name: idx_cascade_batch; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_cascade_batch ON public.cascade_events USING btree (batch_id);


--
-- Name: idx_coa_dock_intim; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_coa_dock_intim ON public.coa_document USING btree (dock_intimation_id);


--
-- Name: idx_coa_lot; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_coa_lot ON public.coa_document USING btree (lot_number);


--
-- Name: idx_coa_qc_intim; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_coa_qc_intim ON public.coa_document USING btree (qc_intimation_id);


--
-- Name: idx_coa_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_coa_status ON public.coa_document USING btree (coa_status);


--
-- Name: idx_coa_supplier; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_coa_supplier ON public.coa_document USING btree (supplier_id);


--
-- Name: idx_coa_uploaded_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_coa_uploaded_at ON public.coa_document USING btree (uploaded_at DESC);


--
-- Name: idx_delivery_endpoint; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_delivery_endpoint ON public.webhook_delivery USING btree (endpoint_id, created_at DESC);


--
-- Name: idx_delivery_event; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_delivery_event ON public.webhook_delivery USING btree (event_id);


--
-- Name: idx_delivery_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_delivery_status ON public.webhook_delivery USING btree (status) WHERE (status = ANY (ARRAY['pending'::text, 'failed'::text]));


--
-- Name: idx_discrepancy_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_discrepancy_entity ON public.discrepancy_report USING btree (entity);


--
-- Name: idx_discrepancy_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_discrepancy_status ON public.discrepancy_report USING btree (status);


--
-- Name: idx_env_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_env_jc ON public.job_card_environment USING btree (job_card_id);


--
-- Name: idx_floor_inv_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_floor_inv_entity ON public.floor_inventory USING btree (entity);


--
-- Name: idx_floor_inv_location; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_floor_inv_location ON public.floor_inventory USING btree (floor_location);


--
-- Name: idx_floor_inv_sku; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_floor_inv_sku ON public.floor_inventory USING btree (sku_name);


--
-- Name: idx_floor_move_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_floor_move_date ON public.floor_movement USING btree (moved_at);


--
-- Name: idx_floor_move_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_floor_move_jc ON public.floor_movement USING btree (job_card_id);


--
-- Name: idx_floor_stock_fulfillment; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_floor_stock_fulfillment ON public.fulfillment_floor_stock USING btree (fulfillment_id);


--
-- Name: idx_floor_stock_material; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_floor_stock_material ON public.fulfillment_floor_stock USING btree (material_sku_name);


--
-- Name: idx_floor_stock_v2_fulfillment; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_floor_stock_v2_fulfillment ON public.fulfillment_floor_stock_v2 USING btree (so_fulfillment_id);


--
-- Name: idx_floor_stock_v2_material; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_floor_stock_v2_material ON public.fulfillment_floor_stock_v2 USING btree (material_sku_name);


--
-- Name: idx_fsl_batch; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_fsl_batch ON public.fifo_skip_log USING btree (batch_id);


--
-- Name: idx_fulfillment_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_fulfillment_entity ON public.so_fulfillment USING btree (entity);


--
-- Name: idx_fulfillment_fy; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_fulfillment_fy ON public.so_fulfillment USING btree (financial_year);


--
-- Name: idx_fulfillment_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_fulfillment_status ON public.so_fulfillment USING btree (order_status);


--
-- Name: idx_gate_passes_issued; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gate_passes_issued ON public.gate_passes USING btree (issued_at);


--
-- Name: idx_gate_passes_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gate_passes_source ON public.gate_passes USING btree (source_ref_type, source_ref_id);


--
-- Name: idx_gate_passes_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gate_passes_type ON public.gate_passes USING btree (gate_pass_type);


--
-- Name: idx_gate_passes_voided; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gate_passes_voided ON public.gate_passes USING btree (voided);


--
-- Name: idx_gate_passes_warehouse; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gate_passes_warehouse ON public.gate_passes USING btree (warehouse);


--
-- Name: idx_gp_sample_req; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gp_sample_req ON public.gate_pass_sample_details USING btree (requisition_id);


--
-- Name: idx_gst_recon_so; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gst_recon_so ON public.so_gst_reconciliation USING btree (so_id);


--
-- Name: idx_gst_recon_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gst_recon_status ON public.so_gst_reconciliation USING btree (status);


--
-- Name: idx_ietl_transfer; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ietl_transfer ON public.inter_entity_transfer_line USING btree (transfer_id);


--
-- Name: idx_iin_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_iin_entity ON public.internal_issue_note USING btree (entity);


--
-- Name: idx_iin_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_iin_status ON public.internal_issue_note USING btree (status);


--
-- Name: idx_indent_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_indent_entity ON public.purchase_indent USING btree (entity);


--
-- Name: idx_indent_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_indent_status ON public.purchase_indent USING btree (status);


--
-- Name: idx_intord_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_intord_status ON public.internal_order USING btree (status);


--
-- Name: idx_inv_batch_blocked_so; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_inv_batch_blocked_so ON public.inventory_batch USING btree (blocked_for_so_id);


--
-- Name: idx_inv_batch_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_inv_batch_entity ON public.inventory_batch USING btree (entity);


--
-- Name: idx_inv_batch_fifo; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_inv_batch_fifo ON public.inventory_batch USING btree (inward_date);


--
-- Name: idx_inv_batch_floor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_inv_batch_floor ON public.inventory_batch USING btree (floor_id);


--
-- Name: idx_inv_batch_sku_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_inv_batch_sku_status ON public.inventory_batch USING btree (sku_name, status, entity);


--
-- Name: idx_inv_batch_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_inv_batch_status ON public.inventory_batch USING btree (status);


--
-- Name: idx_inv_event_batch; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_inv_event_batch ON public.inventory_event_log USING btree (batch_id);


--
-- Name: idx_inv_event_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_inv_event_type ON public.inventory_event_log USING btree (event_type);


--
-- Name: idx_isn_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_isn_jc ON public.issue_note USING btree (job_card_id);


--
-- Name: idx_isn_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_isn_status ON public.issue_note USING btree (status);


--
-- Name: idx_isnl_note; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_isnl_note ON public.issue_note_line USING btree (issue_note_id);


--
-- Name: idx_jc_additives_v2_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jc_additives_v2_jc ON public.job_card_additive_consumption_v2 USING btree (job_card_id);


--
-- Name: idx_jc_env_not_deleted; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jc_env_not_deleted ON public.job_card_environment USING btree (job_card_id) WHERE (deleted_at IS NULL);


--
-- Name: idx_jc_loss_not_deleted; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jc_loss_not_deleted ON public.job_card_loss_reconciliation USING btree (job_card_id) WHERE (deleted_at IS NULL);


--
-- Name: idx_jc_metal_not_deleted; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jc_metal_not_deleted ON public.job_card_metal_detection USING btree (job_card_id) WHERE (deleted_at IS NULL);


--
-- Name: idx_jc_remarks_not_deleted; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jc_remarks_not_deleted ON public.job_card_remarks USING btree (job_card_id) WHERE (deleted_at IS NULL);


--
-- Name: idx_jc_v2_chain_next; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jc_v2_chain_next ON public.job_card_v2 USING btree (next_job_card_id) WHERE (next_job_card_id IS NOT NULL);


--
-- Name: idx_jc_v2_chain_prev; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jc_v2_chain_prev ON public.job_card_v2 USING btree (prev_job_card_id) WHERE (prev_job_card_id IS NOT NULL);


--
-- Name: idx_jc_v2_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jc_v2_created_at ON public.job_card_v2 USING btree (created_at DESC) WHERE (deleted_at IS NULL);


--
-- Name: idx_jc_v2_end_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jc_v2_end_time ON public.job_card_v2 USING btree (end_time DESC) WHERE ((deleted_at IS NULL) AND (end_time IS NOT NULL));


--
-- Name: idx_jc_v2_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jc_v2_entity ON public.job_card_v2 USING btree (entity);


--
-- Name: idx_jc_v2_factory; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jc_v2_factory ON public.job_card_v2 USING btree (factory);


--
-- Name: idx_jc_v2_floor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jc_v2_floor ON public.job_card_v2 USING btree (floor) WHERE (floor IS NOT NULL);


--
-- Name: idx_jc_v2_floor_open; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jc_v2_floor_open ON public.job_card_v2 USING btree (floor, factory) WHERE ((deleted_at IS NULL) AND (status <> ALL (ARRAY['closed'::text, 'cancelled'::text])));


--
-- Name: idx_jc_v2_force_closed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jc_v2_force_closed ON public.job_card_v2 USING btree (force_close_at DESC) WHERE (force_closed = true);


--
-- Name: idx_jc_v2_plan; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jc_v2_plan ON public.job_card_v2 USING btree (plan_id);


--
-- Name: idx_jc_v2_plan_line; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jc_v2_plan_line ON public.job_card_v2 USING btree (plan_line_id);


--
-- Name: idx_jc_v2_start_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jc_v2_start_time ON public.job_card_v2 USING btree (start_time DESC) WHERE ((deleted_at IS NULL) AND (start_time IS NOT NULL));


--
-- Name: idx_jc_v2_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jc_v2_status ON public.job_card_v2 USING btree (status);


--
-- Name: idx_jc_wc_not_deleted; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jc_wc_not_deleted ON public.job_card_weight_check USING btree (job_card_id) WHERE (deleted_at IS NULL);


--
-- Name: idx_jca_v2_batch; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jca_v2_batch ON public.job_card_accounting_v2 USING btree (batch_id);


--
-- Name: idx_jca_v2_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jca_v2_jc ON public.job_card_accounting_v2 USING btree (job_card_id);


--
-- Name: idx_jca_v2_jc_batch_unbalanced; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jca_v2_jc_batch_unbalanced ON public.job_card_accounting_v2 USING btree (job_card_id, batch_id) WHERE (is_balanced = false);


--
-- Name: idx_jcac_v2_batch; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcac_v2_batch ON public.job_card_additive_consumption_v2 USING btree (batch_id);


--
-- Name: idx_jcbm_v2_batch; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcbm_v2_batch ON public.job_card_balance_material_v2 USING btree (batch_id);


--
-- Name: idx_jcbm_v2_bom_line; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcbm_v2_bom_line ON public.job_card_balance_material_v2 USING btree (bom_line_id);


--
-- Name: idx_jcbm_v2_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcbm_v2_jc ON public.job_card_balance_material_v2 USING btree (job_card_id);


--
-- Name: idx_jcbp_v2_batch; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcbp_v2_batch ON public.job_card_byproducts_v2 USING btree (batch_id);


--
-- Name: idx_jcbp_v2_bom_line; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcbp_v2_bom_line ON public.job_card_byproducts_v2 USING btree (bom_line_id);


--
-- Name: idx_jcbp_v2_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcbp_v2_jc ON public.job_card_byproducts_v2 USING btree (job_card_id);


--
-- Name: idx_jcenv_v2_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcenv_v2_active ON public.job_card_environment_v2 USING btree (job_card_id) WHERE (deleted_at IS NULL);


--
-- Name: idx_jcenv_v2_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcenv_v2_jc ON public.job_card_environment_v2 USING btree (job_card_id);


--
-- Name: idx_jcex_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcex_jc ON public.jc_material_exception_v2 USING btree (job_card_id);


--
-- Name: idx_jcex_request; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcex_request ON public.jc_material_exception_v2 USING btree (request_id);


--
-- Name: idx_jcloss_v2_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcloss_v2_active ON public.job_card_loss_reconciliation_v2 USING btree (job_card_id) WHERE (deleted_at IS NULL);


--
-- Name: idx_jcloss_v2_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcloss_v2_jc ON public.job_card_loss_reconciliation_v2 USING btree (job_card_id);


--
-- Name: idx_jcmc_v2_batch; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcmc_v2_batch ON public.job_card_material_consumption_v2 USING btree (batch_id);


--
-- Name: idx_jcmc_v2_bom_line; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcmc_v2_bom_line ON public.job_card_material_consumption_v2 USING btree (bom_line_id);


--
-- Name: idx_jcmc_v2_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcmc_v2_jc ON public.job_card_material_consumption_v2 USING btree (job_card_id);


--
-- Name: idx_jcmc_v2_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcmc_v2_kind ON public.job_card_material_consumption_v2 USING btree (input_kind);


--
-- Name: idx_jcmd_v2_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcmd_v2_active ON public.job_card_metal_detection_v2 USING btree (job_card_id) WHERE (deleted_at IS NULL);


--
-- Name: idx_jcmd_v2_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcmd_v2_jc ON public.job_card_metal_detection_v2 USING btree (job_card_id);


--
-- Name: idx_jco_v2_batch; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jco_v2_batch ON public.job_card_output_v2 USING btree (batch_id);


--
-- Name: idx_jco_v2_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jco_v2_jc ON public.job_card_output_v2 USING btree (job_card_id);


--
-- Name: idx_jco_v2_phase; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jco_v2_phase ON public.job_card_output_v2 USING btree (phase_id);


--
-- Name: idx_jcpd_from; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcpd_from ON public.job_card_partial_dispatch USING btree (from_job_card_id);


--
-- Name: idx_jcpd_to; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcpd_to ON public.job_card_partial_dispatch USING btree (to_job_card_id);


--
-- Name: idx_jcpd_v2_batch; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcpd_v2_batch ON public.job_card_partial_dispatch_v2 USING btree (batch_id);


--
-- Name: idx_jcpd_v2_from; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcpd_v2_from ON public.job_card_partial_dispatch_v2 USING btree (from_job_card_id);


--
-- Name: idx_jcpd_v2_phase; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcpd_v2_phase ON public.job_card_partial_dispatch_v2 USING btree (phase_id);


--
-- Name: idx_jcpd_v2_to; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcpd_v2_to ON public.job_card_partial_dispatch_v2 USING btree (to_job_card_id);


--
-- Name: idx_jcphase_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcphase_date ON public.job_card_phase_v2 USING btree (phase_date);


--
-- Name: idx_jcphase_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcphase_jc ON public.job_card_phase_v2 USING btree (job_card_id);


--
-- Name: idx_jcphase_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcphase_status ON public.job_card_phase_v2 USING btree (status);


--
-- Name: idx_jcqc_v2_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcqc_v2_jc ON public.job_card_qc_v2 USING btree (job_card_id);


--
-- Name: idx_jcqc_v2_result; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcqc_v2_result ON public.job_card_qc_v2 USING btree (result);


--
-- Name: idx_jcrem_v2_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcrem_v2_active ON public.job_card_remarks_v2 USING btree (job_card_id) WHERE (deleted_at IS NULL);


--
-- Name: idx_jcrem_v2_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcrem_v2_jc ON public.job_card_remarks_v2 USING btree (job_card_id);


--
-- Name: idx_jcsl_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcsl_jc ON public.job_card_shift_log USING btree (job_card_id);


--
-- Name: idx_jcsl_open; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcsl_open ON public.job_card_shift_log USING btree (job_card_id) WHERE (end_at IS NULL);


--
-- Name: idx_jcsl_v2_batch; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcsl_v2_batch ON public.job_card_shift_log_v2 USING btree (batch_id);


--
-- Name: idx_jcsl_v2_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcsl_v2_jc ON public.job_card_shift_log_v2 USING btree (job_card_id);


--
-- Name: idx_jcsl_v2_open; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcsl_v2_open ON public.job_card_shift_log_v2 USING btree (job_card_id) WHERE (end_at IS NULL);


--
-- Name: idx_jcsl_v2_phase; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcsl_v2_phase ON public.job_card_shift_log_v2 USING btree (phase_id);


--
-- Name: idx_jcso_v2_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcso_v2_jc ON public.job_card_sign_off_v2 USING btree (job_card_id);


--
-- Name: idx_jcvar_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcvar_jc ON public.job_card_consumption_variance_v2 USING btree (job_card_id);


--
-- Name: idx_jcvar_material; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcvar_material ON public.job_card_consumption_variance_v2 USING btree (material_sku_name);


--
-- Name: idx_jcvar_recorded_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcvar_recorded_at ON public.job_card_consumption_variance_v2 USING btree (recorded_at);


--
-- Name: idx_jcwc_v2_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcwc_v2_active ON public.job_card_weight_check_v2 USING btree (job_card_id) WHERE (deleted_at IS NULL);


--
-- Name: idx_jcwc_v2_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_jcwc_v2_jc ON public.job_card_weight_check_v2 USING btree (job_card_id);


--
-- Name: idx_lb_batch; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_lb_batch ON public.lot_block USING btree (batch_id, is_active);


--
-- Name: idx_lb_lot; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_lb_lot ON public.lot_block USING btree (lot_number, is_active);


--
-- Name: idx_lb_so; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_lb_so ON public.lot_block USING btree (blocked_for_so) WHERE (is_active = true);


--
-- Name: idx_legacy_log_batch; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_legacy_log_batch ON public.legacy_import_log USING btree (batch_id);


--
-- Name: idx_log_edit_changed_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_log_edit_changed_at ON public.log_edit USING btree (changed_at);


--
-- Name: idx_log_edit_record; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_log_edit_record ON public.log_edit USING btree (table_name, record_id);


--
-- Name: idx_loss_recon_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_loss_recon_jc ON public.job_card_loss_reconciliation USING btree (job_card_id);


--
-- Name: idx_machine_capacity_machine; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_machine_capacity_machine ON public.machine_capacity USING btree (machine_id);


--
-- Name: idx_machine_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_machine_entity ON public.machine USING btree (entity);


--
-- Name: idx_machine_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_machine_status ON public.machine USING btree (status);


--
-- Name: idx_mat_consumption_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_mat_consumption_jc ON public.job_card_material_consumption USING btree (job_card_id);


--
-- Name: idx_matdoc_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_matdoc_date ON public.material_document USING btree (posting_date);


--
-- Name: idx_matdoc_mvt; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_matdoc_mvt ON public.material_document USING btree (movement_type);


--
-- Name: idx_matdoc_ref; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_matdoc_ref ON public.material_document USING btree (reference_type, reference_id);


--
-- Name: idx_matdoc_sample_req; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_matdoc_sample_req ON public.material_document USING btree (sample_requisition_id) WHERE (sample_requisition_id IS NOT NULL);


--
-- Name: idx_matdocl_batch; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_matdocl_batch ON public.material_document_line USING btree (batch_id);


--
-- Name: idx_matdocl_doc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_matdocl_doc ON public.material_document_line USING btree (mat_doc_id);


--
-- Name: idx_metal_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_metal_jc ON public.job_card_metal_detection USING btree (job_card_id);


--
-- Name: idx_ncr_capa_ncr; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ncr_capa_ncr ON public.ncr_supplier_action USING btree (ncr_no);


--
-- Name: idx_ncr_capa_unverified; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ncr_capa_unverified ON public.ncr_supplier_action USING btree (ncr_no) WHERE (verified_at IS NULL);


--
-- Name: idx_ncr_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ncr_entity ON public.ncr_record USING btree (entity);


--
-- Name: idx_ncr_event_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ncr_event_entity ON public.ncr_event_log USING btree (entity_id, occurred_at DESC);


--
-- Name: idx_ncr_event_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ncr_event_type ON public.ncr_event_log USING btree (event_type, occurred_at DESC);


--
-- Name: idx_ncr_inspection; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ncr_inspection ON public.ncr_record USING btree (inspection_id);


--
-- Name: idx_ncr_param_ncr; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ncr_param_ncr ON public.ncr_parameter_detail USING btree (ncr_no);


--
-- Name: idx_ncr_raised_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ncr_raised_at ON public.ncr_record USING btree (raised_at DESC);


--
-- Name: idx_ncr_severity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ncr_severity ON public.ncr_record USING btree (severity);


--
-- Name: idx_ncr_sku; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ncr_sku ON public.ncr_record USING btree (sku_id);


--
-- Name: idx_ncr_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ncr_status ON public.ncr_record USING btree (status);


--
-- Name: idx_ncr_supplier; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ncr_supplier ON public.ncr_record USING btree (supplier_id);


--
-- Name: idx_ncr_transaction; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ncr_transaction ON public.ncr_record USING btree (transaction_no);


--
-- Name: idx_npd_authorized_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_npd_authorized_active ON public.npd_authorized_users USING btree (capability, active);


--
-- Name: idx_npd_dev_jc_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_npd_dev_jc_created ON public.npd_dev_job_cards USING btree (created_at);


--
-- Name: idx_npd_dev_jc_lines; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_npd_dev_jc_lines ON public.npd_dev_job_card_lines USING btree (dev_jc_id);


--
-- Name: idx_npd_dev_jc_lines_phase; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_npd_dev_jc_lines_phase ON public.npd_dev_job_card_lines USING btree (phase_id);


--
-- Name: idx_npd_dev_jc_phases; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_npd_dev_jc_phases ON public.npd_dev_job_card_phases USING btree (dev_jc_id);


--
-- Name: idx_npd_dev_jc_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_npd_dev_jc_status ON public.npd_dev_job_cards USING btree (status);


--
-- Name: idx_npd_draft_bom_lines_draft; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_npd_draft_bom_lines_draft ON public.npd_draft_bom_lines USING btree (draft_bom_id);


--
-- Name: idx_npd_draft_boms_req; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_npd_draft_boms_req ON public.npd_draft_boms USING btree (requisition_id);


--
-- Name: idx_npd_draft_boms_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_npd_draft_boms_status ON public.npd_draft_boms USING btree (status);


--
-- Name: idx_offgrade_cons_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_offgrade_cons_jc ON public.offgrade_consumption USING btree (job_card_id);


--
-- Name: idx_offgrade_group; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_offgrade_group ON public.offgrade_inventory USING btree (item_group);


--
-- Name: idx_offgrade_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_offgrade_status ON public.offgrade_inventory USING btree (status);


--
-- Name: idx_ogi_disp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ogi_disp ON public.off_grade_inventory USING btree (disposition);


--
-- Name: idx_ogi_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ogi_entity ON public.off_grade_inventory USING btree (entity);


--
-- Name: idx_plan_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_plan_date ON public.production_plan USING btree (plan_date);


--
-- Name: idx_plan_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_plan_entity ON public.production_plan USING btree (entity);


--
-- Name: idx_plan_line_plan; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_plan_line_plan ON public.production_plan_line USING btree (plan_id);


--
-- Name: idx_plan_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_plan_status ON public.production_plan USING btree (status);


--
-- Name: idx_pm_indent_bom_line; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pm_indent_bom_line ON public.job_card_pm_indent USING btree (bom_line_id);


--
-- Name: idx_pm_indent_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pm_indent_jc ON public.job_card_pm_indent USING btree (job_card_id);


--
-- Name: idx_pm_indent_v2_bom_line; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pm_indent_v2_bom_line ON public.job_card_pm_indent_v2 USING btree (bom_line_id);


--
-- Name: idx_pm_v2_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pm_v2_jc ON public.job_card_pm_indent_v2 USING btree (job_card_id);


--
-- Name: idx_po_box_txn; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_po_box_txn ON public.po_box USING btree (transaction_no);


--
-- Name: idx_po_event_log_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_po_event_log_entity ON public.po_event_log USING btree (entity, occurred_at DESC);


--
-- Name: idx_po_event_log_txn; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_po_event_log_txn ON public.po_event_log USING btree (transaction_no, occurred_at DESC);


--
-- Name: idx_po_header_deleted_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_po_header_deleted_at ON public.po_header USING btree (deleted_at);


--
-- Name: idx_po_header_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_po_header_entity ON public.po_header USING btree (entity);


--
-- Name: idx_po_header_po_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_po_header_po_date ON public.po_header USING btree (po_date);


--
-- Name: idx_po_header_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_po_header_status ON public.po_header USING btree (status);


--
-- Name: idx_po_line_sku; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_po_line_sku ON public.po_line USING btree (sku_name);


--
-- Name: idx_po_section_txn; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_po_section_txn ON public.po_section USING btree (transaction_no);


--
-- Name: idx_pp_v2_entity_wh_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pp_v2_entity_wh_date ON public.production_plan_v2 USING btree (entity, warehouse, plan_date);


--
-- Name: idx_pp_v2_revision_chain; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pp_v2_revision_chain ON public.production_plan_v2 USING btree (previous_plan_id) WHERE (previous_plan_id IS NOT NULL);


--
-- Name: idx_pp_v2_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pp_v2_status ON public.production_plan_v2 USING btree (status);


--
-- Name: idx_ppl_v2_bom; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ppl_v2_bom ON public.production_plan_line_v2 USING btree (bom_id);


--
-- Name: idx_ppl_v2_fg_customer; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ppl_v2_fg_customer ON public.production_plan_line_v2 USING btree (fg_sku_name, customer_name);


--
-- Name: idx_ppl_v2_linked_so_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ppl_v2_linked_so_gin ON public.production_plan_line_v2 USING gin (linked_so_fulfillment_ids);


--
-- Name: idx_ppl_v2_plan; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ppl_v2_plan ON public.production_plan_line_v2 USING btree (plan_id);


--
-- Name: idx_ppl_v2_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_ppl_v2_status ON public.production_plan_line_v2 USING btree (status);


--
-- Name: idx_pps_v2_line; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_pps_v2_line ON public.production_plan_step_v2 USING btree (plan_line_id);


--
-- Name: idx_prdi_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_prdi_created ON public.production_indent USING btree (created_at DESC);


--
-- Name: idx_prdi_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_prdi_entity ON public.production_indent USING btree (entity);


--
-- Name: idx_prdi_item_so; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_prdi_item_so ON public.production_indent USING btree (item_description, triggered_by_so);


--
-- Name: idx_prdi_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_prdi_status ON public.production_indent USING btree (status);


--
-- Name: idx_process_loss_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_process_loss_date ON public.process_loss USING btree (production_date);


--
-- Name: idx_process_loss_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_process_loss_entity ON public.process_loss USING btree (entity);


--
-- Name: idx_process_loss_product; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_process_loss_product ON public.process_loss USING btree (product_name);


--
-- Name: idx_prod_order_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_prod_order_entity ON public.production_order USING btree (entity);


--
-- Name: idx_prod_order_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_prod_order_status ON public.production_order USING btree (status);


--
-- Name: idx_qc_intimation_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_qc_intimation_status ON public.qc_intimation USING btree (status);


--
-- Name: idx_qc_inward_audit_inspection; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_qc_inward_audit_inspection ON public.qc_inward_inspection_audit USING btree (inspection_id);


--
-- Name: idx_qc_inward_intimation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_qc_inward_intimation ON public.qc_inward_inspection USING btree (qc_intimation_id);


--
-- Name: idx_qc_inward_reading_inspection; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_qc_inward_reading_inspection ON public.qc_inward_reading USING btree (inspection_id);


--
-- Name: idx_qc_inward_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_qc_inward_status ON public.qc_inward_inspection USING btree (status);


--
-- Name: idx_qci_checkpoint; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_qci_checkpoint ON public.qc_inspection USING btree (checkpoint_type);


--
-- Name: idx_qci_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_qci_jc ON public.qc_inspection USING btree (job_card_id);


--
-- Name: idx_qci_result; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_qci_result ON public.qc_inspection USING btree (result);


--
-- Name: idx_qcnotif_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_qcnotif_jc ON public.qc_notification_log_v2 USING btree (job_card_id);


--
-- Name: idx_qcnotif_recipient; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_qcnotif_recipient ON public.qc_notification_log_v2 USING btree (recipient_user_id);


--
-- Name: idx_qcnotif_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_qcnotif_status ON public.qc_notification_log_v2 USING btree (delivery_status);


--
-- Name: idx_qi_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_qi_jc ON public.quality_inspection USING btree (job_card_id);


--
-- Name: idx_qi_result; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_qi_result ON public.quality_inspection USING btree (result);


--
-- Name: idx_recon_fail_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_recon_fail_entity ON public.reconciliation_failures USING btree (entity);


--
-- Name: idx_recv_doc_dock_intim; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_recv_doc_dock_intim ON public.receipt_document USING btree (dock_intimation_id);


--
-- Name: idx_recv_doc_supplier; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_recv_doc_supplier ON public.receipt_document USING btree (supplier_id);


--
-- Name: idx_recv_doc_txn; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_recv_doc_txn ON public.receipt_document USING btree (transaction_no);


--
-- Name: idx_recv_doc_uploaded_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_recv_doc_uploaded_at ON public.receipt_document USING btree (uploaded_at DESC);


--
-- Name: idx_refresh_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_refresh_active ON public.auth_refresh_token USING btree (user_id) WHERE ((revoked_at IS NULL) AND (rotated_at IS NULL));


--
-- Name: idx_refresh_chain_root; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_refresh_chain_root ON public.auth_refresh_token USING btree (chain_root);


--
-- Name: idx_refresh_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_refresh_user ON public.auth_refresh_token USING btree (user_id);


--
-- Name: idx_reject_log_batch; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_reject_log_batch ON public.batch_rejection_log USING btree (batch_id);


--
-- Name: idx_remarks_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_remarks_jc ON public.job_card_remarks USING btree (job_card_id);


--
-- Name: idx_revision_fulfillment; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_revision_fulfillment ON public.so_revision_log USING btree (fulfillment_id);


--
-- Name: idx_rm_indent_bom_line; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rm_indent_bom_line ON public.job_card_rm_indent USING btree (bom_line_id);


--
-- Name: idx_rm_indent_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rm_indent_jc ON public.job_card_rm_indent USING btree (job_card_id);


--
-- Name: idx_rm_indent_v2_bom_line; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rm_indent_v2_bom_line ON public.job_card_rm_indent_v2 USING btree (bom_line_id);


--
-- Name: idx_rm_issue_form_lines; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rm_issue_form_lines ON public.rm_issue_form_lines USING btree (form_id);


--
-- Name: idx_rm_issue_form_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rm_issue_form_source ON public.rm_issue_form USING btree (source_type, source_id);


--
-- Name: idx_rm_issue_form_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rm_issue_form_status ON public.rm_issue_form USING btree (status);


--
-- Name: idx_rm_v2_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rm_v2_jc ON public.job_card_rm_indent_v2 USING btree (job_card_id);


--
-- Name: idx_rtvd_rtv; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rtvd_rtv ON public.rtv_disposition USING btree (rtv_id);


--
-- Name: idx_rtvd_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rtvd_type ON public.rtv_disposition USING btree (disposition_type);


--
-- Name: idx_sample_approvals_approver; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sample_approvals_approver ON public.sample_approvals USING btree (approver_user_id);


--
-- Name: idx_sample_approvals_req; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sample_approvals_req ON public.sample_approvals USING btree (requisition_id);


--
-- Name: idx_sample_audit_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sample_audit_created_at ON public.sample_audit_log USING btree (created_at);


--
-- Name: idx_sample_audit_req; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sample_audit_req ON public.sample_audit_log USING btree (requisition_id);


--
-- Name: idx_sample_cons_var_src; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sample_cons_var_src ON public.sample_consumption_variance USING btree (source_type, source_id);


--
-- Name: idx_sample_req_articles_req; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sample_req_articles_req ON public.sample_requisition_articles USING btree (requisition_id);


--
-- Name: idx_sample_req_articles_sku; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sample_req_articles_sku ON public.sample_requisition_articles USING btree (sku_id);


--
-- Name: idx_sample_req_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sample_req_created_at ON public.sample_requisitions USING btree (created_at);


--
-- Name: idx_sample_req_requestor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sample_req_requestor ON public.sample_requisitions USING btree (requestor_user_id);


--
-- Name: idx_sample_req_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sample_req_status ON public.sample_requisitions USING btree (status) WHERE (deleted_at IS NULL);


--
-- Name: idx_sample_req_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sample_req_type ON public.sample_requisitions USING btree (sample_type) WHERE (deleted_at IS NULL);


--
-- Name: idx_sample_req_warehouse; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sample_req_warehouse ON public.sample_requisitions USING btree (warehouse);


--
-- Name: idx_sfg_box_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sfg_box_jc ON public.sfg_box USING btree (job_card_id);


--
-- Name: idx_sfg_box_recv; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sfg_box_recv ON public.sfg_box USING btree (received_into_job_card_id);


--
-- Name: idx_sfg_box_sku; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sfg_box_sku ON public.sfg_box USING btree (sfg_code, status);


--
-- Name: idx_sign_off_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sign_off_jc ON public.job_card_sign_off USING btree (job_card_id);


--
-- Name: idx_sof_v2_carryforward_from; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sof_v2_carryforward_from ON public.so_fulfillment_v2 USING btree (carryforward_from_id) WHERE (carryforward_from_id IS NOT NULL);


--
-- Name: idx_sof_v2_entity_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sof_v2_entity_status ON public.so_fulfillment_v2 USING btree (entity, order_status);


--
-- Name: idx_sof_v2_fg_customer; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sof_v2_fg_customer ON public.so_fulfillment_v2 USING btree (fg_sku_name, customer_name);


--
-- Name: idx_srl_v2_fulfillment; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_srl_v2_fulfillment ON public.so_revision_log_v2 USING btree (so_fulfillment_id);


--
-- Name: idx_store_alloc_decision; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_store_alloc_decision ON public.store_allocation USING btree (decision);


--
-- Name: idx_store_alloc_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_store_alloc_entity ON public.store_allocation USING btree (entity);


--
-- Name: idx_store_alloc_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_store_alloc_jc ON public.store_allocation USING btree (job_card_id);


--
-- Name: idx_vbh_bank; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_vbh_bank ON public.vendor_banking_history USING btree (bank_id, changed_at DESC);


--
-- Name: idx_vbh_vendor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_vbh_vendor ON public.vendor_banking_history USING btree (vendor_id, changed_at DESC);


--
-- Name: idx_vch_contract; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_vch_contract ON public.vendor_contract_history USING btree (contract_id, changed_at DESC);


--
-- Name: idx_vch_vendor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_vch_vendor ON public.vendor_contract_history USING btree (vendor_id, changed_at DESC);


--
-- Name: idx_vdh_doc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_vdh_doc ON public.vendor_document_history USING btree (doc_id, changed_at DESC);


--
-- Name: idx_vdh_vendor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_vdh_vendor ON public.vendor_document_history USING btree (vendor_id, changed_at DESC);


--
-- Name: idx_vendor_staging_created_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_vendor_staging_created_by ON public.vendor_extraction_staging USING btree (created_by, created_at DESC);


--
-- Name: idx_vendor_staging_expires; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_vendor_staging_expires ON public.vendor_extraction_staging USING btree (expires_at) WHERE (consumed_at IS NULL);


--
-- Name: idx_vmh_changed_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_vmh_changed_at ON public.vendor_master_history USING btree (changed_at DESC);


--
-- Name: idx_vmh_vendor; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_vmh_vendor ON public.vendor_master_history USING btree (vendor_id, changed_at DESC);


--
-- Name: idx_weight_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_weight_jc ON public.job_card_weight_check USING btree (job_card_id);


--
-- Name: idx_yield_period; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_yield_period ON public.yield_summary USING btree (period);


--
-- Name: idx_yield_product; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_yield_product ON public.yield_summary USING btree (product_name);


--
-- Name: ix_promote_appr_req; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_promote_appr_req ON public.npd_dev_promote_approval USING btree (promote_request_id);


--
-- Name: ix_wa_promote_message_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_wa_promote_message_jc ON public.wa_promote_message USING btree (dev_jc_id);


--
-- Name: ix_wa_review_message_req; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_wa_review_message_req ON public.wa_review_message USING btree (requisition_id);


--
-- Name: uq_bom_header_active_fg; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_bom_header_active_fg ON public.bom_header USING btree (fg_sku_name) WHERE (is_active = true);


--
-- Name: uq_bom_header_fg_version; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_bom_header_fg_version ON public.bom_header USING btree (fg_sku_name, version) WHERE (is_active = true);


--
-- Name: uq_byproducts_jc_batch_cat_mat; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_byproducts_jc_batch_cat_mat ON public.job_card_byproducts_v2 USING btree (job_card_id, COALESCE(batch_id, (0)::bigint), category, COALESCE(material_name, ''::text));


--
-- Name: uq_jca_v2_jc_batch; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_jca_v2_jc_batch ON public.job_card_accounting_v2 USING btree (job_card_id, COALESCE(batch_id, (0)::bigint));


--
-- Name: uq_jcbm_v2_jc_batch_bom_type; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_jcbm_v2_jc_batch_bom_type ON public.job_card_balance_material_v2 USING btree (job_card_id, COALESCE(batch_id, (0)::bigint), COALESCE(bom_line_id, 0), balance_type);


--
-- Name: uq_jcmc_v2_jc_batch_material; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_jcmc_v2_jc_batch_material ON public.job_card_material_consumption_v2 USING btree (job_card_id, COALESCE(batch_id, (0)::bigint), material_sku_name);


--
-- Name: uq_jcsl_v2_one_open; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_jcsl_v2_one_open ON public.job_card_shift_log_v2 USING btree (job_card_id) WHERE (end_at IS NULL);


--
-- Name: uq_jcwc_v2_jc_sample_active; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_jcwc_v2_jc_sample_active ON public.job_card_weight_check_v2 USING btree (job_card_id, sample_number) WHERE (deleted_at IS NULL);


--
-- Name: uq_po_header_live_entity_pono; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_po_header_live_entity_pono ON public.po_header USING btree (entity, po_number) WHERE ((deleted_at IS NULL) AND (po_number IS NOT NULL));


--
-- Name: uq_promote_appr_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_promote_appr_kind ON public.npd_dev_promote_approval USING btree (promote_request_id, approver_kind);


--
-- Name: uq_promote_req_live; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_promote_req_live ON public.npd_dev_promote_request USING btree (dev_jc_id) WHERE (status = 'PENDING'::text);


--
-- Name: uq_qc_parameter_code; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_qc_parameter_code ON public.qc_parameter USING btree (code);


--
-- Name: fulfillment_bom_override_v2 trg_bom_override_v2_touch; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_bom_override_v2_touch BEFORE UPDATE ON public.fulfillment_bom_override_v2 FOR EACH ROW EXECUTE FUNCTION public.fn_touch_updated_at();


--
-- Name: fulfillment_floor_stock_v2 trg_floor_stock_v2_touch; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_floor_stock_v2_touch BEFORE UPDATE ON public.fulfillment_floor_stock_v2 FOR EACH ROW EXECUTE FUNCTION public.fn_touch_updated_at();


--
-- Name: job_card_v2 trg_jc_v2_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_jc_v2_updated_at BEFORE UPDATE ON public.job_card_v2 FOR EACH ROW EXECUTE FUNCTION public.fn_touch_updated_at();


--
-- Name: job_card_accounting_v2 trg_jca_v2_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_jca_v2_updated_at BEFORE UPDATE ON public.job_card_accounting_v2 FOR EACH ROW EXECUTE FUNCTION public.fn_touch_updated_at();


--
-- Name: job_card_environment_v2 trg_jcenv_v2_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_jcenv_v2_updated_at BEFORE UPDATE ON public.job_card_environment_v2 FOR EACH ROW EXECUTE FUNCTION public.fn_touch_updated_at();


--
-- Name: job_card_loss_reconciliation_v2 trg_jcloss_v2_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_jcloss_v2_updated_at BEFORE UPDATE ON public.job_card_loss_reconciliation_v2 FOR EACH ROW EXECUTE FUNCTION public.fn_touch_updated_at();


--
-- Name: job_card_metal_detection_v2 trg_jcmd_v2_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_jcmd_v2_updated_at BEFORE UPDATE ON public.job_card_metal_detection_v2 FOR EACH ROW EXECUTE FUNCTION public.fn_touch_updated_at();


--
-- Name: job_card_qc_v2 trg_jcqc_v2_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_jcqc_v2_updated_at BEFORE UPDATE ON public.job_card_qc_v2 FOR EACH ROW EXECUTE FUNCTION public.fn_touch_updated_at();


--
-- Name: job_card_remarks_v2 trg_jcrem_v2_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_jcrem_v2_updated_at BEFORE UPDATE ON public.job_card_remarks_v2 FOR EACH ROW EXECUTE FUNCTION public.fn_touch_updated_at();


--
-- Name: job_card_consumption_variance_v2 trg_jcvar_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_jcvar_updated_at BEFORE UPDATE ON public.job_card_consumption_variance_v2 FOR EACH ROW EXECUTE FUNCTION public.fn_touch_jcvar_last_updated();


--
-- Name: job_card_weight_check_v2 trg_jcwc_v2_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_jcwc_v2_updated_at BEFORE UPDATE ON public.job_card_weight_check_v2 FOR EACH ROW EXECUTE FUNCTION public.fn_touch_updated_at();


--
-- Name: production_plan_step_v2 trg_pps_v2_touch; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_pps_v2_touch BEFORE UPDATE ON public.production_plan_step_v2 FOR EACH ROW EXECUTE FUNCTION public.fn_touch_updated_at();


--
-- Name: so_fulfillment_v2 trg_sof_v2_touch; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_sof_v2_touch BEFORE UPDATE ON public.so_fulfillment_v2 FOR EACH ROW EXECUTE FUNCTION public.fn_touch_updated_at();


--
-- Name: job_card_partial_dispatch_v2 trg_sync_phase_batch_id_dispatch; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_sync_phase_batch_id_dispatch BEFORE INSERT OR UPDATE ON public.job_card_partial_dispatch_v2 FOR EACH ROW EXECUTE FUNCTION public.fn_sync_phase_batch_id();


--
-- Name: job_card_output_v2 trg_sync_phase_batch_id_output; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_sync_phase_batch_id_output BEFORE INSERT OR UPDATE ON public.job_card_output_v2 FOR EACH ROW EXECUTE FUNCTION public.fn_sync_phase_batch_id();


--
-- Name: job_card_shift_log_v2 trg_sync_phase_batch_id_shift; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_sync_phase_batch_id_shift BEFORE INSERT OR UPDATE ON public.job_card_shift_log_v2 FOR EACH ROW EXECUTE FUNCTION public.fn_sync_phase_batch_id();


--
-- Name: vendor_banking_history trg_vbh_no_update; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_vbh_no_update BEFORE UPDATE ON public.vendor_banking_history FOR EACH ROW EXECUTE FUNCTION public.vendor_history_block_update();


--
-- Name: vendor_contract_history trg_vch_no_update; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_vch_no_update BEFORE UPDATE ON public.vendor_contract_history FOR EACH ROW EXECUTE FUNCTION public.vendor_history_block_update();


--
-- Name: vendor_document_history trg_vdh_no_update; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_vdh_no_update BEFORE UPDATE ON public.vendor_document_history FOR EACH ROW EXECUTE FUNCTION public.vendor_history_block_update();


--
-- Name: vendor_master_history trg_vmh_no_update; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_vmh_no_update BEFORE UPDATE ON public.vendor_master_history FOR EACH ROW EXECUTE FUNCTION public.vendor_history_block_update();


--
-- Name: ai_recommendation ai_recommendation_plan_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_recommendation
    ADD CONSTRAINT ai_recommendation_plan_id_fkey FOREIGN KEY (plan_id) REFERENCES public.production_plan(plan_id);


--
-- Name: auth_password_reset_otp auth_password_reset_otp_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_password_reset_otp
    ADD CONSTRAINT auth_password_reset_otp_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.auth_user(user_id) ON DELETE CASCADE;


--
-- Name: auth_refresh_token auth_refresh_token_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_refresh_token
    ADD CONSTRAINT auth_refresh_token_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.auth_user(user_id) ON DELETE CASCADE;


--
-- Name: auth_role_permission auth_role_permission_permission_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_role_permission
    ADD CONSTRAINT auth_role_permission_permission_id_fkey FOREIGN KEY (permission_id) REFERENCES public.auth_permission(permission_id) ON DELETE CASCADE;


--
-- Name: auth_role_permission auth_role_permission_role_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_role_permission
    ADD CONSTRAINT auth_role_permission_role_id_fkey FOREIGN KEY (role_id) REFERENCES public.auth_role(role_id) ON DELETE CASCADE;


--
-- Name: auth_session auth_session_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_session
    ADD CONSTRAINT auth_session_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.auth_user(user_id);


--
-- Name: auth_user auth_user_role_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_user
    ADD CONSTRAINT auth_user_role_id_fkey FOREIGN KEY (role_id) REFERENCES public.auth_role(role_id);


--
-- Name: auth_user_role auth_user_role_role_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_user_role
    ADD CONSTRAINT auth_user_role_role_id_fkey FOREIGN KEY (role_id) REFERENCES public.auth_role(role_id) ON DELETE CASCADE;


--
-- Name: auth_user_role auth_user_role_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_user_role
    ADD CONSTRAINT auth_user_role_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.auth_user(user_id) ON DELETE CASCADE;


--
-- Name: bom_amendment_request_v2 bom_amendment_request_v2_bom_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bom_amendment_request_v2
    ADD CONSTRAINT bom_amendment_request_v2_bom_id_fkey FOREIGN KEY (bom_id) REFERENCES public.bom_header(bom_id);


--
-- Name: bom_amendment_request_v2 bom_amendment_request_v2_checker1_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bom_amendment_request_v2
    ADD CONSTRAINT bom_amendment_request_v2_checker1_user_id_fkey FOREIGN KEY (checker1_user_id) REFERENCES public.auth_user(user_id);


--
-- Name: bom_amendment_request_v2 bom_amendment_request_v2_checker2_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bom_amendment_request_v2
    ADD CONSTRAINT bom_amendment_request_v2_checker2_user_id_fkey FOREIGN KEY (checker2_user_id) REFERENCES public.auth_user(user_id);


--
-- Name: bom_amendment_request_v2 bom_amendment_request_v2_job_card_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bom_amendment_request_v2
    ADD CONSTRAINT bom_amendment_request_v2_job_card_id_fkey FOREIGN KEY (job_card_id) REFERENCES public.job_card_v2(job_card_id);


--
-- Name: bom_amendment_request_v2 bom_amendment_request_v2_maker_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bom_amendment_request_v2
    ADD CONSTRAINT bom_amendment_request_v2_maker_user_id_fkey FOREIGN KEY (maker_user_id) REFERENCES public.auth_user(user_id);


--
-- Name: bom_line bom_line_bom_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bom_line
    ADD CONSTRAINT bom_line_bom_id_fkey FOREIGN KEY (bom_id) REFERENCES public.bom_header(bom_id);


--
-- Name: bom_process_route bom_process_route_bom_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bom_process_route
    ADD CONSTRAINT bom_process_route_bom_id_fkey FOREIGN KEY (bom_id) REFERENCES public.bom_header(bom_id);


--
-- Name: coa_document coa_document_replaces_coa_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.coa_document
    ADD CONSTRAINT coa_document_replaces_coa_id_fkey FOREIGN KEY (replaces_coa_id) REFERENCES public.coa_document(coa_id);


--
-- Name: day_end_balance_scan_line day_end_balance_scan_line_scan_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.day_end_balance_scan_line
    ADD CONSTRAINT day_end_balance_scan_line_scan_id_fkey FOREIGN KEY (scan_id) REFERENCES public.day_end_balance_scan(scan_id);


--
-- Name: discrepancy_report discrepancy_report_affected_machine_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.discrepancy_report
    ADD CONSTRAINT discrepancy_report_affected_machine_id_fkey FOREIGN KEY (affected_machine_id) REFERENCES public.machine(machine_id);


--
-- Name: sample_requisitions fk_sample_req_gate_pass; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_requisitions
    ADD CONSTRAINT fk_sample_req_gate_pass FOREIGN KEY (linked_gate_pass_id) REFERENCES public.gate_passes(id);


--
-- Name: sample_requisitions fk_sample_req_npd_draft; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_requisitions
    ADD CONSTRAINT fk_sample_req_npd_draft FOREIGN KEY (npd_draft_bom_id) REFERENCES public.npd_draft_boms(id);


--
-- Name: vendor_master_history fk_vmh_vendor; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_master_history
    ADD CONSTRAINT fk_vmh_vendor FOREIGN KEY (vendor_id) REFERENCES public.vendor_master(vendor_id) ON DELETE CASCADE;


--
-- Name: fulfillment_bom_override fulfillment_bom_override_fulfillment_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fulfillment_bom_override
    ADD CONSTRAINT fulfillment_bom_override_fulfillment_id_fkey FOREIGN KEY (fulfillment_id) REFERENCES public.so_fulfillment(fulfillment_id);


--
-- Name: fulfillment_bom_override_v2 fulfillment_bom_override_v2_so_fulfillment_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fulfillment_bom_override_v2
    ADD CONSTRAINT fulfillment_bom_override_v2_so_fulfillment_id_fkey FOREIGN KEY (so_fulfillment_id) REFERENCES public.so_fulfillment_v2(so_fulfillment_id) ON DELETE CASCADE;


--
-- Name: fulfillment_floor_stock fulfillment_floor_stock_fulfillment_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fulfillment_floor_stock
    ADD CONSTRAINT fulfillment_floor_stock_fulfillment_id_fkey FOREIGN KEY (fulfillment_id) REFERENCES public.so_fulfillment(fulfillment_id);


--
-- Name: fulfillment_floor_stock_v2 fulfillment_floor_stock_v2_so_fulfillment_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.fulfillment_floor_stock_v2
    ADD CONSTRAINT fulfillment_floor_stock_v2_so_fulfillment_id_fkey FOREIGN KEY (so_fulfillment_id) REFERENCES public.so_fulfillment_v2(so_fulfillment_id) ON DELETE CASCADE;


--
-- Name: gate_pass_sample_details gate_pass_sample_details_gate_pass_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gate_pass_sample_details
    ADD CONSTRAINT gate_pass_sample_details_gate_pass_id_fkey FOREIGN KEY (gate_pass_id) REFERENCES public.gate_passes(id) ON DELETE CASCADE;


--
-- Name: gate_pass_sample_details gate_pass_sample_details_original_requisition_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gate_pass_sample_details
    ADD CONSTRAINT gate_pass_sample_details_original_requisition_id_fkey FOREIGN KEY (original_requisition_id) REFERENCES public.sample_requisitions(id);


--
-- Name: gate_pass_sample_details gate_pass_sample_details_requisition_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gate_pass_sample_details
    ADD CONSTRAINT gate_pass_sample_details_requisition_id_fkey FOREIGN KEY (requisition_id) REFERENCES public.sample_requisitions(id);


--
-- Name: gate_passes gate_passes_approver1_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gate_passes
    ADD CONSTRAINT gate_passes_approver1_user_id_fkey FOREIGN KEY (approver1_user_id) REFERENCES public.auth_user(user_id);


--
-- Name: gate_passes gate_passes_approver2_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gate_passes
    ADD CONSTRAINT gate_passes_approver2_user_id_fkey FOREIGN KEY (approver2_user_id) REFERENCES public.auth_user(user_id);


--
-- Name: gate_passes gate_passes_issued_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gate_passes
    ADD CONSTRAINT gate_passes_issued_by_fkey FOREIGN KEY (issued_by) REFERENCES public.auth_user(user_id);


--
-- Name: gate_passes gate_passes_material_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gate_passes
    ADD CONSTRAINT gate_passes_material_document_id_fkey FOREIGN KEY (material_document_id) REFERENCES public.material_document(id);


--
-- Name: gate_passes gate_passes_voided_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gate_passes
    ADD CONSTRAINT gate_passes_voided_by_fkey FOREIGN KEY (voided_by) REFERENCES public.auth_user(user_id);


--
-- Name: inter_entity_transfer_line inter_entity_transfer_line_transfer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inter_entity_transfer_line
    ADD CONSTRAINT inter_entity_transfer_line_transfer_id_fkey FOREIGN KEY (transfer_id) REFERENCES public.inter_entity_transfer(transfer_id);


--
-- Name: internal_order internal_order_prod_indent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.internal_order
    ADD CONSTRAINT internal_order_prod_indent_id_fkey FOREIGN KEY (prod_indent_id) REFERENCES public.production_indent(prod_indent_id);


--
-- Name: issue_note_line issue_note_line_issue_note_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.issue_note_line
    ADD CONSTRAINT issue_note_line_issue_note_id_fkey FOREIGN KEY (issue_note_id) REFERENCES public.issue_note(issue_note_id);


--
-- Name: jc_material_exception_v2 jc_material_exception_v2_job_card_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jc_material_exception_v2
    ADD CONSTRAINT jc_material_exception_v2_job_card_id_fkey FOREIGN KEY (job_card_id) REFERENCES public.job_card_v2(job_card_id) ON DELETE CASCADE;


--
-- Name: jc_material_exception_v2 jc_material_exception_v2_request_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jc_material_exception_v2
    ADD CONSTRAINT jc_material_exception_v2_request_id_fkey FOREIGN KEY (request_id) REFERENCES public.bom_amendment_request_v2(request_id);


--
-- Name: job_card_accounting_v2 job_card_accounting_v2_batch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_accounting_v2
    ADD CONSTRAINT job_card_accounting_v2_batch_id_fkey FOREIGN KEY (batch_id) REFERENCES public.job_card_phase_v2(phase_id);


--
-- Name: job_card_accounting_v2 job_card_accounting_v2_job_card_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_accounting_v2
    ADD CONSTRAINT job_card_accounting_v2_job_card_id_fkey FOREIGN KEY (job_card_id) REFERENCES public.job_card_v2(job_card_id) ON DELETE CASCADE;


--
-- Name: job_card_additive_consumption_v2 job_card_additive_consumption_v2_batch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_additive_consumption_v2
    ADD CONSTRAINT job_card_additive_consumption_v2_batch_id_fkey FOREIGN KEY (batch_id) REFERENCES public.job_card_phase_v2(phase_id);


--
-- Name: job_card_additive_consumption_v2 job_card_additive_consumption_v2_job_card_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_additive_consumption_v2
    ADD CONSTRAINT job_card_additive_consumption_v2_job_card_id_fkey FOREIGN KEY (job_card_id) REFERENCES public.job_card_v2(job_card_id) ON DELETE CASCADE;


--
-- Name: job_card_balance_material_v2 job_card_balance_material_v2_batch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_balance_material_v2
    ADD CONSTRAINT job_card_balance_material_v2_batch_id_fkey FOREIGN KEY (batch_id) REFERENCES public.job_card_phase_v2(phase_id);


--
-- Name: job_card_balance_material_v2 job_card_balance_material_v2_bom_line_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_balance_material_v2
    ADD CONSTRAINT job_card_balance_material_v2_bom_line_id_fkey FOREIGN KEY (bom_line_id) REFERENCES public.bom_line(bom_line_id);


--
-- Name: job_card_balance_material_v2 job_card_balance_material_v2_job_card_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_balance_material_v2
    ADD CONSTRAINT job_card_balance_material_v2_job_card_id_fkey FOREIGN KEY (job_card_id) REFERENCES public.job_card_v2(job_card_id) ON DELETE CASCADE;


--
-- Name: job_card_byproducts_v2 job_card_byproducts_v2_batch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_byproducts_v2
    ADD CONSTRAINT job_card_byproducts_v2_batch_id_fkey FOREIGN KEY (batch_id) REFERENCES public.job_card_phase_v2(phase_id);


--
-- Name: job_card_byproducts_v2 job_card_byproducts_v2_bom_line_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_byproducts_v2
    ADD CONSTRAINT job_card_byproducts_v2_bom_line_id_fkey FOREIGN KEY (bom_line_id) REFERENCES public.bom_line(bom_line_id) ON DELETE SET NULL;


--
-- Name: job_card_byproducts_v2 job_card_byproducts_v2_job_card_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_byproducts_v2
    ADD CONSTRAINT job_card_byproducts_v2_job_card_id_fkey FOREIGN KEY (job_card_id) REFERENCES public.job_card_v2(job_card_id) ON DELETE CASCADE;


--
-- Name: job_card_consumption_variance_v2 job_card_consumption_variance_v2_bom_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_consumption_variance_v2
    ADD CONSTRAINT job_card_consumption_variance_v2_bom_id_fkey FOREIGN KEY (bom_id) REFERENCES public.bom_header(bom_id);


--
-- Name: job_card_consumption_variance_v2 job_card_consumption_variance_v2_job_card_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_consumption_variance_v2
    ADD CONSTRAINT job_card_consumption_variance_v2_job_card_id_fkey FOREIGN KEY (job_card_id) REFERENCES public.job_card_v2(job_card_id) ON DELETE CASCADE;


--
-- Name: job_card_environment_v2 job_card_environment_v2_job_card_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_environment_v2
    ADD CONSTRAINT job_card_environment_v2_job_card_id_fkey FOREIGN KEY (job_card_id) REFERENCES public.job_card_v2(job_card_id) ON DELETE CASCADE;


--
-- Name: job_card_loss_reconciliation_v2 job_card_loss_reconciliation_v2_job_card_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_loss_reconciliation_v2
    ADD CONSTRAINT job_card_loss_reconciliation_v2_job_card_id_fkey FOREIGN KEY (job_card_id) REFERENCES public.job_card_v2(job_card_id) ON DELETE CASCADE;


--
-- Name: job_card_material_consumption_v2 job_card_material_consumption_v2_batch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_material_consumption_v2
    ADD CONSTRAINT job_card_material_consumption_v2_batch_id_fkey FOREIGN KEY (batch_id) REFERENCES public.job_card_phase_v2(phase_id);


--
-- Name: job_card_material_consumption_v2 job_card_material_consumption_v2_bom_line_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_material_consumption_v2
    ADD CONSTRAINT job_card_material_consumption_v2_bom_line_id_fkey FOREIGN KEY (bom_line_id) REFERENCES public.bom_line(bom_line_id);


--
-- Name: job_card_material_consumption_v2 job_card_material_consumption_v2_job_card_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_material_consumption_v2
    ADD CONSTRAINT job_card_material_consumption_v2_job_card_id_fkey FOREIGN KEY (job_card_id) REFERENCES public.job_card_v2(job_card_id) ON DELETE CASCADE;


--
-- Name: job_card_material_consumption_v2 job_card_material_consumption_v2_source_dispatch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_material_consumption_v2
    ADD CONSTRAINT job_card_material_consumption_v2_source_dispatch_id_fkey FOREIGN KEY (source_dispatch_id) REFERENCES public.job_card_partial_dispatch_v2(dispatch_id) ON DELETE SET NULL;


--
-- Name: job_card_material_consumption_v2 job_card_material_consumption_v2_source_rm_indent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_material_consumption_v2
    ADD CONSTRAINT job_card_material_consumption_v2_source_rm_indent_id_fkey FOREIGN KEY (source_rm_indent_id) REFERENCES public.job_card_rm_indent_v2(rm_indent_id) ON DELETE SET NULL;


--
-- Name: job_card_metal_detection_v2 job_card_metal_detection_v2_job_card_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_metal_detection_v2
    ADD CONSTRAINT job_card_metal_detection_v2_job_card_id_fkey FOREIGN KEY (job_card_id) REFERENCES public.job_card_v2(job_card_id) ON DELETE CASCADE;


--
-- Name: job_card_output_v2 job_card_output_v2_batch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_output_v2
    ADD CONSTRAINT job_card_output_v2_batch_id_fkey FOREIGN KEY (batch_id) REFERENCES public.job_card_phase_v2(phase_id);


--
-- Name: job_card_output_v2 job_card_output_v2_job_card_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_output_v2
    ADD CONSTRAINT job_card_output_v2_job_card_id_fkey FOREIGN KEY (job_card_id) REFERENCES public.job_card_v2(job_card_id) ON DELETE CASCADE;


--
-- Name: job_card_output_v2 job_card_output_v2_phase_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_output_v2
    ADD CONSTRAINT job_card_output_v2_phase_id_fkey FOREIGN KEY (phase_id) REFERENCES public.job_card_phase_v2(phase_id);


--
-- Name: job_card_partial_dispatch_v2 job_card_partial_dispatch_v2_batch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_partial_dispatch_v2
    ADD CONSTRAINT job_card_partial_dispatch_v2_batch_id_fkey FOREIGN KEY (batch_id) REFERENCES public.job_card_phase_v2(phase_id);


--
-- Name: job_card_partial_dispatch_v2 job_card_partial_dispatch_v2_from_job_card_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_partial_dispatch_v2
    ADD CONSTRAINT job_card_partial_dispatch_v2_from_job_card_id_fkey FOREIGN KEY (from_job_card_id) REFERENCES public.job_card_v2(job_card_id) ON DELETE CASCADE;


--
-- Name: job_card_partial_dispatch_v2 job_card_partial_dispatch_v2_phase_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_partial_dispatch_v2
    ADD CONSTRAINT job_card_partial_dispatch_v2_phase_id_fkey FOREIGN KEY (phase_id) REFERENCES public.job_card_phase_v2(phase_id);


--
-- Name: job_card_partial_dispatch_v2 job_card_partial_dispatch_v2_to_job_card_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_partial_dispatch_v2
    ADD CONSTRAINT job_card_partial_dispatch_v2_to_job_card_id_fkey FOREIGN KEY (to_job_card_id) REFERENCES public.job_card_v2(job_card_id) ON DELETE CASCADE;


--
-- Name: job_card_phase_v2 job_card_phase_v2_job_card_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_phase_v2
    ADD CONSTRAINT job_card_phase_v2_job_card_id_fkey FOREIGN KEY (job_card_id) REFERENCES public.job_card_v2(job_card_id) ON DELETE CASCADE;


--
-- Name: job_card_pm_indent_v2 job_card_pm_indent_v2_bom_line_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_pm_indent_v2
    ADD CONSTRAINT job_card_pm_indent_v2_bom_line_id_fkey FOREIGN KEY (bom_line_id) REFERENCES public.bom_line(bom_line_id);


--
-- Name: job_card_pm_indent_v2 job_card_pm_indent_v2_job_card_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_pm_indent_v2
    ADD CONSTRAINT job_card_pm_indent_v2_job_card_id_fkey FOREIGN KEY (job_card_id) REFERENCES public.job_card_v2(job_card_id) ON DELETE CASCADE;


--
-- Name: job_card_qc_v2 job_card_qc_v2_job_card_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_qc_v2
    ADD CONSTRAINT job_card_qc_v2_job_card_id_fkey FOREIGN KEY (job_card_id) REFERENCES public.job_card_v2(job_card_id) ON DELETE CASCADE;


--
-- Name: job_card_remarks_v2 job_card_remarks_v2_job_card_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_remarks_v2
    ADD CONSTRAINT job_card_remarks_v2_job_card_id_fkey FOREIGN KEY (job_card_id) REFERENCES public.job_card_v2(job_card_id) ON DELETE CASCADE;


--
-- Name: job_card_rm_indent_v2 job_card_rm_indent_v2_bom_line_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_rm_indent_v2
    ADD CONSTRAINT job_card_rm_indent_v2_bom_line_id_fkey FOREIGN KEY (bom_line_id) REFERENCES public.bom_line(bom_line_id);


--
-- Name: job_card_rm_indent_v2 job_card_rm_indent_v2_job_card_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_rm_indent_v2
    ADD CONSTRAINT job_card_rm_indent_v2_job_card_id_fkey FOREIGN KEY (job_card_id) REFERENCES public.job_card_v2(job_card_id) ON DELETE CASCADE;


--
-- Name: job_card_shift_log_v2 job_card_shift_log_v2_batch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_shift_log_v2
    ADD CONSTRAINT job_card_shift_log_v2_batch_id_fkey FOREIGN KEY (batch_id) REFERENCES public.job_card_phase_v2(phase_id);


--
-- Name: job_card_shift_log_v2 job_card_shift_log_v2_job_card_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_shift_log_v2
    ADD CONSTRAINT job_card_shift_log_v2_job_card_id_fkey FOREIGN KEY (job_card_id) REFERENCES public.job_card_v2(job_card_id) ON DELETE CASCADE;


--
-- Name: job_card_shift_log_v2 job_card_shift_log_v2_phase_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_shift_log_v2
    ADD CONSTRAINT job_card_shift_log_v2_phase_id_fkey FOREIGN KEY (phase_id) REFERENCES public.job_card_phase_v2(phase_id);


--
-- Name: job_card_sign_off_v2 job_card_sign_off_v2_job_card_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_sign_off_v2
    ADD CONSTRAINT job_card_sign_off_v2_job_card_id_fkey FOREIGN KEY (job_card_id) REFERENCES public.job_card_v2(job_card_id) ON DELETE CASCADE;


--
-- Name: job_card_v2 job_card_v2_bom_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_v2
    ADD CONSTRAINT job_card_v2_bom_id_fkey FOREIGN KEY (bom_id) REFERENCES public.bom_header(bom_id);


--
-- Name: job_card_v2 job_card_v2_force_close_request_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_v2
    ADD CONSTRAINT job_card_v2_force_close_request_id_fkey FOREIGN KEY (force_close_request_id) REFERENCES public.bom_amendment_request_v2(request_id);


--
-- Name: job_card_v2 job_card_v2_machine_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_v2
    ADD CONSTRAINT job_card_v2_machine_id_fkey FOREIGN KEY (machine_id) REFERENCES public.machine(machine_id);


--
-- Name: job_card_v2 job_card_v2_next_job_card_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_v2
    ADD CONSTRAINT job_card_v2_next_job_card_id_fkey FOREIGN KEY (next_job_card_id) REFERENCES public.job_card_v2(job_card_id);


--
-- Name: job_card_v2 job_card_v2_plan_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_v2
    ADD CONSTRAINT job_card_v2_plan_id_fkey FOREIGN KEY (plan_id) REFERENCES public.production_plan_v2(plan_id) ON DELETE RESTRICT;


--
-- Name: job_card_v2 job_card_v2_plan_line_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_v2
    ADD CONSTRAINT job_card_v2_plan_line_id_fkey FOREIGN KEY (plan_line_id) REFERENCES public.production_plan_line_v2(plan_line_id) ON DELETE RESTRICT;


--
-- Name: job_card_v2 job_card_v2_plan_step_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_v2
    ADD CONSTRAINT job_card_v2_plan_step_id_fkey FOREIGN KEY (plan_step_id) REFERENCES public.production_plan_step_v2(step_id) ON DELETE RESTRICT;


--
-- Name: job_card_v2 job_card_v2_prev_job_card_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_v2
    ADD CONSTRAINT job_card_v2_prev_job_card_id_fkey FOREIGN KEY (prev_job_card_id) REFERENCES public.job_card_v2(job_card_id);


--
-- Name: job_card_weight_check_v2 job_card_weight_check_v2_job_card_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.job_card_weight_check_v2
    ADD CONSTRAINT job_card_weight_check_v2_job_card_id_fkey FOREIGN KEY (job_card_id) REFERENCES public.job_card_v2(job_card_id) ON DELETE CASCADE;


--
-- Name: machine_capacity machine_capacity_machine_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.machine_capacity
    ADD CONSTRAINT machine_capacity_machine_id_fkey FOREIGN KEY (machine_id) REFERENCES public.machine(machine_id);


--
-- Name: material_document material_document_gate_pass_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.material_document
    ADD CONSTRAINT material_document_gate_pass_id_fkey FOREIGN KEY (gate_pass_id) REFERENCES public.gate_passes(id);


--
-- Name: material_document_line material_document_line_mat_doc_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.material_document_line
    ADD CONSTRAINT material_document_line_mat_doc_id_fkey FOREIGN KEY (mat_doc_id) REFERENCES public.material_document(mat_doc_id);


--
-- Name: material_document material_document_sample_requisition_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.material_document
    ADD CONSTRAINT material_document_sample_requisition_id_fkey FOREIGN KEY (sample_requisition_id) REFERENCES public.sample_requisitions(id);


--
-- Name: ncr_parameter_detail ncr_parameter_detail_ncr_no_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ncr_parameter_detail
    ADD CONSTRAINT ncr_parameter_detail_ncr_no_fkey FOREIGN KEY (ncr_no) REFERENCES public.ncr_record(ncr_no) ON DELETE CASCADE;


--
-- Name: ncr_supplier_action ncr_supplier_action_ncr_no_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ncr_supplier_action
    ADD CONSTRAINT ncr_supplier_action_ncr_no_fkey FOREIGN KEY (ncr_no) REFERENCES public.ncr_record(ncr_no) ON DELETE CASCADE;


--
-- Name: npd_authorized_users npd_authorized_users_granted_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_authorized_users
    ADD CONSTRAINT npd_authorized_users_granted_by_fkey FOREIGN KEY (granted_by) REFERENCES public.auth_user(user_id);


--
-- Name: npd_authorized_users npd_authorized_users_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_authorized_users
    ADD CONSTRAINT npd_authorized_users_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.auth_user(user_id);


--
-- Name: npd_dev_job_card_lines npd_dev_job_card_lines_dev_jc_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_dev_job_card_lines
    ADD CONSTRAINT npd_dev_job_card_lines_dev_jc_id_fkey FOREIGN KEY (dev_jc_id) REFERENCES public.npd_dev_job_cards(id) ON DELETE CASCADE;


--
-- Name: npd_dev_job_card_lines npd_dev_job_card_lines_phase_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_dev_job_card_lines
    ADD CONSTRAINT npd_dev_job_card_lines_phase_id_fkey FOREIGN KEY (phase_id) REFERENCES public.npd_dev_job_card_phases(phase_id) ON DELETE CASCADE;


--
-- Name: npd_dev_job_card_lines npd_dev_job_card_lines_sku_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_dev_job_card_lines
    ADD CONSTRAINT npd_dev_job_card_lines_sku_id_fkey FOREIGN KEY (sku_id) REFERENCES public.all_sku(sku_id);


--
-- Name: npd_dev_job_card_phases npd_dev_job_card_phases_completed_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_dev_job_card_phases
    ADD CONSTRAINT npd_dev_job_card_phases_completed_by_fkey FOREIGN KEY (completed_by) REFERENCES public.auth_user(user_id);


--
-- Name: npd_dev_job_card_phases npd_dev_job_card_phases_dev_jc_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_dev_job_card_phases
    ADD CONSTRAINT npd_dev_job_card_phases_dev_jc_id_fkey FOREIGN KEY (dev_jc_id) REFERENCES public.npd_dev_job_cards(id) ON DELETE CASCADE;


--
-- Name: npd_dev_job_card_phases npd_dev_job_card_phases_started_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_dev_job_card_phases
    ADD CONSTRAINT npd_dev_job_card_phases_started_by_fkey FOREIGN KEY (started_by) REFERENCES public.auth_user(user_id);


--
-- Name: npd_dev_job_cards npd_dev_job_cards_base_bom_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_dev_job_cards
    ADD CONSTRAINT npd_dev_job_cards_base_bom_id_fkey FOREIGN KEY (base_bom_id) REFERENCES public.bom_header(bom_id);


--
-- Name: npd_dev_job_cards npd_dev_job_cards_closed_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_dev_job_cards
    ADD CONSTRAINT npd_dev_job_cards_closed_by_fkey FOREIGN KEY (closed_by) REFERENCES public.auth_user(user_id);


--
-- Name: npd_dev_job_cards npd_dev_job_cards_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_dev_job_cards
    ADD CONSTRAINT npd_dev_job_cards_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.auth_user(user_id);


--
-- Name: npd_dev_job_cards npd_dev_job_cards_dispatched_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_dev_job_cards
    ADD CONSTRAINT npd_dev_job_cards_dispatched_by_fkey FOREIGN KEY (dispatched_by) REFERENCES public.auth_user(user_id);


--
-- Name: npd_dev_job_cards npd_dev_job_cards_fg_sku_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_dev_job_cards
    ADD CONSTRAINT npd_dev_job_cards_fg_sku_id_fkey FOREIGN KEY (fg_sku_id) REFERENCES public.all_sku(sku_id);


--
-- Name: npd_dev_job_cards npd_dev_job_cards_promoted_bom_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_dev_job_cards
    ADD CONSTRAINT npd_dev_job_cards_promoted_bom_id_fkey FOREIGN KEY (promoted_bom_id) REFERENCES public.bom_header(bom_id);


--
-- Name: npd_dev_job_cards npd_dev_job_cards_started_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_dev_job_cards
    ADD CONSTRAINT npd_dev_job_cards_started_by_fkey FOREIGN KEY (started_by) REFERENCES public.auth_user(user_id);


--
-- Name: npd_dev_promote_approval npd_dev_promote_approval_approver_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_dev_promote_approval
    ADD CONSTRAINT npd_dev_promote_approval_approver_user_id_fkey FOREIGN KEY (approver_user_id) REFERENCES public.auth_user(user_id);


--
-- Name: npd_dev_promote_approval npd_dev_promote_approval_promote_request_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_dev_promote_approval
    ADD CONSTRAINT npd_dev_promote_approval_promote_request_id_fkey FOREIGN KEY (promote_request_id) REFERENCES public.npd_dev_promote_request(id) ON DELETE CASCADE;


--
-- Name: npd_dev_promote_request npd_dev_promote_request_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_dev_promote_request
    ADD CONSTRAINT npd_dev_promote_request_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.auth_user(user_id);


--
-- Name: npd_dev_promote_request npd_dev_promote_request_dev_jc_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_dev_promote_request
    ADD CONSTRAINT npd_dev_promote_request_dev_jc_id_fkey FOREIGN KEY (dev_jc_id) REFERENCES public.npd_dev_job_cards(id) ON DELETE CASCADE;


--
-- Name: npd_draft_bom_lines npd_draft_bom_lines_draft_bom_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_draft_bom_lines
    ADD CONSTRAINT npd_draft_bom_lines_draft_bom_id_fkey FOREIGN KEY (draft_bom_id) REFERENCES public.npd_draft_boms(id) ON DELETE CASCADE;


--
-- Name: npd_draft_bom_lines npd_draft_bom_lines_sku_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_draft_bom_lines
    ADD CONSTRAINT npd_draft_bom_lines_sku_id_fkey FOREIGN KEY (sku_id) REFERENCES public.all_sku(sku_id);


--
-- Name: npd_draft_boms npd_draft_boms_base_bom_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_draft_boms
    ADD CONSTRAINT npd_draft_boms_base_bom_id_fkey FOREIGN KEY (base_bom_id) REFERENCES public.bom_header(bom_id);


--
-- Name: npd_draft_boms npd_draft_boms_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_draft_boms
    ADD CONSTRAINT npd_draft_boms_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.auth_user(user_id);


--
-- Name: npd_draft_boms npd_draft_boms_fg_sku_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_draft_boms
    ADD CONSTRAINT npd_draft_boms_fg_sku_id_fkey FOREIGN KEY (fg_sku_id) REFERENCES public.all_sku(sku_id);


--
-- Name: npd_draft_boms npd_draft_boms_promoted_bom_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_draft_boms
    ADD CONSTRAINT npd_draft_boms_promoted_bom_id_fkey FOREIGN KEY (promoted_bom_id) REFERENCES public.bom_header(bom_id);


--
-- Name: npd_draft_boms npd_draft_boms_promoted_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_draft_boms
    ADD CONSTRAINT npd_draft_boms_promoted_by_fkey FOREIGN KEY (promoted_by) REFERENCES public.auth_user(user_id);


--
-- Name: npd_draft_boms npd_draft_boms_promotion_approval_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_draft_boms
    ADD CONSTRAINT npd_draft_boms_promotion_approval_id_fkey FOREIGN KEY (promotion_approval_id) REFERENCES public.sample_approvals(id);


--
-- Name: npd_draft_boms npd_draft_boms_requisition_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.npd_draft_boms
    ADD CONSTRAINT npd_draft_boms_requisition_id_fkey FOREIGN KEY (requisition_id) REFERENCES public.sample_requisitions(id) ON DELETE CASCADE;


--
-- Name: offgrade_consumption offgrade_consumption_offgrade_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.offgrade_consumption
    ADD CONSTRAINT offgrade_consumption_offgrade_id_fkey FOREIGN KEY (offgrade_id) REFERENCES public.offgrade_inventory(offgrade_id);


--
-- Name: po_box po_box_transaction_no_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.po_box
    ADD CONSTRAINT po_box_transaction_no_fkey FOREIGN KEY (transaction_no) REFERENCES public.po_header(transaction_no);


--
-- Name: po_box po_box_transaction_no_line_number_section_number_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.po_box
    ADD CONSTRAINT po_box_transaction_no_line_number_section_number_fkey FOREIGN KEY (transaction_no, line_number, section_number) REFERENCES public.po_section(transaction_no, line_number, section_number);


--
-- Name: po_line po_line_transaction_no_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.po_line
    ADD CONSTRAINT po_line_transaction_no_fkey FOREIGN KEY (transaction_no) REFERENCES public.po_header(transaction_no);


--
-- Name: po_section po_section_transaction_no_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.po_section
    ADD CONSTRAINT po_section_transaction_no_fkey FOREIGN KEY (transaction_no) REFERENCES public.po_header(transaction_no);


--
-- Name: po_section po_section_transaction_no_line_number_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.po_section
    ADD CONSTRAINT po_section_transaction_no_line_number_fkey FOREIGN KEY (transaction_no, line_number) REFERENCES public.po_line(transaction_no, line_number);


--
-- Name: production_order production_order_bom_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.production_order
    ADD CONSTRAINT production_order_bom_id_fkey FOREIGN KEY (bom_id) REFERENCES public.bom_header(bom_id);


--
-- Name: production_order production_order_machine_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.production_order
    ADD CONSTRAINT production_order_machine_id_fkey FOREIGN KEY (machine_id) REFERENCES public.machine(machine_id);


--
-- Name: production_order production_order_plan_line_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.production_order
    ADD CONSTRAINT production_order_plan_line_id_fkey FOREIGN KEY (plan_line_id) REFERENCES public.production_plan_line(plan_line_id);


--
-- Name: production_plan_line production_plan_line_bom_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.production_plan_line
    ADD CONSTRAINT production_plan_line_bom_id_fkey FOREIGN KEY (bom_id) REFERENCES public.bom_header(bom_id);


--
-- Name: production_plan_line production_plan_line_machine_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.production_plan_line
    ADD CONSTRAINT production_plan_line_machine_id_fkey FOREIGN KEY (machine_id) REFERENCES public.machine(machine_id);


--
-- Name: production_plan_line production_plan_line_plan_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.production_plan_line
    ADD CONSTRAINT production_plan_line_plan_id_fkey FOREIGN KEY (plan_id) REFERENCES public.production_plan(plan_id);


--
-- Name: production_plan_line_v2 production_plan_line_v2_bom_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.production_plan_line_v2
    ADD CONSTRAINT production_plan_line_v2_bom_id_fkey FOREIGN KEY (bom_id) REFERENCES public.bom_header(bom_id) ON DELETE RESTRICT;


--
-- Name: production_plan_line_v2 production_plan_line_v2_plan_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.production_plan_line_v2
    ADD CONSTRAINT production_plan_line_v2_plan_id_fkey FOREIGN KEY (plan_id) REFERENCES public.production_plan_v2(plan_id) ON DELETE CASCADE;


--
-- Name: production_plan production_plan_previous_plan_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.production_plan
    ADD CONSTRAINT production_plan_previous_plan_id_fkey FOREIGN KEY (previous_plan_id) REFERENCES public.production_plan(plan_id);


--
-- Name: production_plan_step_v2 production_plan_step_v2_plan_line_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.production_plan_step_v2
    ADD CONSTRAINT production_plan_step_v2_plan_line_id_fkey FOREIGN KEY (plan_line_id) REFERENCES public.production_plan_line_v2(plan_line_id) ON DELETE CASCADE;


--
-- Name: production_plan_v2 production_plan_v2_previous_plan_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.production_plan_v2
    ADD CONSTRAINT production_plan_v2_previous_plan_id_fkey FOREIGN KEY (previous_plan_id) REFERENCES public.production_plan_v2(plan_id) ON DELETE SET NULL;


--
-- Name: purchase_indent purchase_indent_plan_line_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.purchase_indent
    ADD CONSTRAINT purchase_indent_plan_line_id_fkey FOREIGN KEY (plan_line_id) REFERENCES public.production_plan_line(plan_line_id);


--
-- Name: qc_inward_inspection_audit qc_inward_inspection_audit_inspection_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qc_inward_inspection_audit
    ADD CONSTRAINT qc_inward_inspection_audit_inspection_id_fkey FOREIGN KEY (inspection_id) REFERENCES public.qc_inward_inspection(inspection_id) ON DELETE CASCADE;


--
-- Name: qc_inward_inspection qc_inward_inspection_qc_intimation_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qc_inward_inspection
    ADD CONSTRAINT qc_inward_inspection_qc_intimation_id_fkey FOREIGN KEY (qc_intimation_id) REFERENCES public.qc_intimation(qc_intimation_id);


--
-- Name: qc_inward_reading qc_inward_reading_inspection_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qc_inward_reading
    ADD CONSTRAINT qc_inward_reading_inspection_id_fkey FOREIGN KEY (inspection_id) REFERENCES public.qc_inward_inspection(inspection_id) ON DELETE CASCADE;


--
-- Name: qc_notification_log_v2 qc_notification_log_v2_dispatched_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qc_notification_log_v2
    ADD CONSTRAINT qc_notification_log_v2_dispatched_by_fkey FOREIGN KEY (dispatched_by) REFERENCES public.auth_user(user_id);


--
-- Name: qc_notification_log_v2 qc_notification_log_v2_job_card_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qc_notification_log_v2
    ADD CONSTRAINT qc_notification_log_v2_job_card_id_fkey FOREIGN KEY (job_card_id) REFERENCES public.job_card_v2(job_card_id) ON DELETE CASCADE;


--
-- Name: qc_notification_log_v2 qc_notification_log_v2_recipient_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qc_notification_log_v2
    ADD CONSTRAINT qc_notification_log_v2_recipient_user_id_fkey FOREIGN KEY (recipient_user_id) REFERENCES public.auth_user(user_id);


--
-- Name: qc_sku_spec qc_sku_spec_parameter_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.qc_sku_spec
    ADD CONSTRAINT qc_sku_spec_parameter_id_fkey FOREIGN KEY (parameter_id) REFERENCES public.qc_parameter(parameter_id);


--
-- Name: rm_issue_form rm_issue_form_issued_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rm_issue_form
    ADD CONSTRAINT rm_issue_form_issued_by_fkey FOREIGN KEY (issued_by) REFERENCES public.auth_user(user_id);


--
-- Name: rm_issue_form_lines rm_issue_form_lines_form_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rm_issue_form_lines
    ADD CONSTRAINT rm_issue_form_lines_form_id_fkey FOREIGN KEY (form_id) REFERENCES public.rm_issue_form(id) ON DELETE CASCADE;


--
-- Name: rm_issue_form_lines rm_issue_form_lines_sku_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rm_issue_form_lines
    ADD CONSTRAINT rm_issue_form_lines_sku_id_fkey FOREIGN KEY (sku_id) REFERENCES public.all_sku(sku_id);


--
-- Name: rm_issue_form rm_issue_form_requested_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rm_issue_form
    ADD CONSTRAINT rm_issue_form_requested_by_fkey FOREIGN KEY (requested_by) REFERENCES public.auth_user(user_id);


--
-- Name: rm_issue_form rm_issue_form_requisition_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rm_issue_form
    ADD CONSTRAINT rm_issue_form_requisition_id_fkey FOREIGN KEY (requisition_id) REFERENCES public.sample_requisitions(id);


--
-- Name: sample_approval_role_map sample_approval_role_map_required_role_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_approval_role_map
    ADD CONSTRAINT sample_approval_role_map_required_role_fkey FOREIGN KEY (required_role) REFERENCES public.auth_role(role_name);


--
-- Name: sample_approvals sample_approvals_approver_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_approvals
    ADD CONSTRAINT sample_approvals_approver_user_id_fkey FOREIGN KEY (approver_user_id) REFERENCES public.auth_user(user_id);


--
-- Name: sample_approvals sample_approvals_requisition_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_approvals
    ADD CONSTRAINT sample_approvals_requisition_id_fkey FOREIGN KEY (requisition_id) REFERENCES public.sample_requisitions(id) ON DELETE CASCADE;


--
-- Name: sample_audit_log sample_audit_log_actor_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_audit_log
    ADD CONSTRAINT sample_audit_log_actor_user_id_fkey FOREIGN KEY (actor_user_id) REFERENCES public.auth_user(user_id);


--
-- Name: sample_audit_log sample_audit_log_requisition_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_audit_log
    ADD CONSTRAINT sample_audit_log_requisition_id_fkey FOREIGN KEY (requisition_id) REFERENCES public.sample_requisitions(id) ON DELETE CASCADE;


--
-- Name: sample_config sample_config_updated_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_config
    ADD CONSTRAINT sample_config_updated_by_fkey FOREIGN KEY (updated_by) REFERENCES public.auth_user(user_id);


--
-- Name: sample_requisition_articles sample_requisition_articles_requisition_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_requisition_articles
    ADD CONSTRAINT sample_requisition_articles_requisition_id_fkey FOREIGN KEY (requisition_id) REFERENCES public.sample_requisitions(id) ON DELETE CASCADE;


--
-- Name: sample_requisition_articles sample_requisition_articles_sku_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_requisition_articles
    ADD CONSTRAINT sample_requisition_articles_sku_id_fkey FOREIGN KEY (sku_id) REFERENCES public.all_sku(sku_id);


--
-- Name: sample_requisitions sample_requisitions_base_bom_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_requisitions
    ADD CONSTRAINT sample_requisitions_base_bom_id_fkey FOREIGN KEY (base_bom_id) REFERENCES public.bom_header(bom_id);


--
-- Name: sample_requisitions sample_requisitions_business_head_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_requisitions
    ADD CONSTRAINT sample_requisitions_business_head_user_id_fkey FOREIGN KEY (business_head_user_id) REFERENCES public.auth_user(user_id);


--
-- Name: sample_requisitions sample_requisitions_converted_from_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_requisitions
    ADD CONSTRAINT sample_requisitions_converted_from_id_fkey FOREIGN KEY (converted_from_id) REFERENCES public.sample_requisitions(id);


--
-- Name: sample_requisitions sample_requisitions_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_requisitions
    ADD CONSTRAINT sample_requisitions_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.auth_user(user_id);


--
-- Name: sample_requisitions sample_requisitions_deleted_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_requisitions
    ADD CONSTRAINT sample_requisitions_deleted_by_fkey FOREIGN KEY (deleted_by) REFERENCES public.auth_user(user_id);


--
-- Name: sample_requisitions sample_requisitions_requestor_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_requisitions
    ADD CONSTRAINT sample_requisitions_requestor_user_id_fkey FOREIGN KEY (requestor_user_id) REFERENCES public.auth_user(user_id);


--
-- Name: sample_requisitions sample_requisitions_updated_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sample_requisitions
    ADD CONSTRAINT sample_requisitions_updated_by_fkey FOREIGN KEY (updated_by) REFERENCES public.auth_user(user_id);


--
-- Name: so_fulfillment so_fulfillment_carryforward_from_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.so_fulfillment
    ADD CONSTRAINT so_fulfillment_carryforward_from_id_fkey FOREIGN KEY (carryforward_from_id) REFERENCES public.so_fulfillment(fulfillment_id);


--
-- Name: so_fulfillment_v2 so_fulfillment_v2_carryforward_from_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.so_fulfillment_v2
    ADD CONSTRAINT so_fulfillment_v2_carryforward_from_id_fkey FOREIGN KEY (carryforward_from_id) REFERENCES public.so_fulfillment_v2(so_fulfillment_id) ON DELETE SET NULL;


--
-- Name: so_gst_reconciliation so_gst_reconciliation_so_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.so_gst_reconciliation
    ADD CONSTRAINT so_gst_reconciliation_so_id_fkey FOREIGN KEY (so_id) REFERENCES public.so_header(so_id);


--
-- Name: so_gst_reconciliation so_gst_reconciliation_so_line_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.so_gst_reconciliation
    ADD CONSTRAINT so_gst_reconciliation_so_line_id_fkey FOREIGN KEY (so_line_id) REFERENCES public.so_line(so_line_id);


--
-- Name: so_line so_line_so_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.so_line
    ADD CONSTRAINT so_line_so_id_fkey FOREIGN KEY (so_id) REFERENCES public.so_header(so_id);


--
-- Name: so_revision_log so_revision_log_fulfillment_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.so_revision_log
    ADD CONSTRAINT so_revision_log_fulfillment_id_fkey FOREIGN KEY (fulfillment_id) REFERENCES public.so_fulfillment(fulfillment_id);


--
-- Name: so_revision_log_v2 so_revision_log_v2_so_fulfillment_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.so_revision_log_v2
    ADD CONSTRAINT so_revision_log_v2_so_fulfillment_id_fkey FOREIGN KEY (so_fulfillment_id) REFERENCES public.so_fulfillment_v2(so_fulfillment_id) ON DELETE CASCADE;


--
-- Name: vendor_extraction_staging vendor_extraction_staging_consumed_vendor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.vendor_extraction_staging
    ADD CONSTRAINT vendor_extraction_staging_consumed_vendor_id_fkey FOREIGN KEY (consumed_vendor_id) REFERENCES public.vendor_master(vendor_id);


--
-- Name: wa_pending_action wa_pending_action_requisition_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wa_pending_action
    ADD CONSTRAINT wa_pending_action_requisition_id_fkey FOREIGN KEY (requisition_id) REFERENCES public.sample_requisitions(id) ON DELETE CASCADE;


--
-- Name: wa_promote_message wa_promote_message_dev_jc_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wa_promote_message
    ADD CONSTRAINT wa_promote_message_dev_jc_id_fkey FOREIGN KEY (dev_jc_id) REFERENCES public.npd_dev_job_cards(id) ON DELETE CASCADE;


--
-- Name: wa_promote_pending wa_promote_pending_dev_jc_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wa_promote_pending
    ADD CONSTRAINT wa_promote_pending_dev_jc_id_fkey FOREIGN KEY (dev_jc_id) REFERENCES public.npd_dev_job_cards(id) ON DELETE CASCADE;


--
-- Name: wa_review_message wa_review_message_requisition_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wa_review_message
    ADD CONSTRAINT wa_review_message_requisition_id_fkey FOREIGN KEY (requisition_id) REFERENCES public.sample_requisitions(id) ON DELETE CASCADE;


--
-- Name: webhook_delivery webhook_delivery_endpoint_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_delivery
    ADD CONSTRAINT webhook_delivery_endpoint_id_fkey FOREIGN KEY (endpoint_id) REFERENCES public.webhook_endpoint(id);


--
-- Name: webhook_subscription webhook_subscription_endpoint_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webhook_subscription
    ADD CONSTRAINT webhook_subscription_endpoint_id_fkey FOREIGN KEY (endpoint_id) REFERENCES public.webhook_endpoint(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict zZLSf7xTEIOc3Wy4WNmRSMJ7krOCfVr4YcjgLcYQ9hgP0xIffbc297vvLpy0QFk

