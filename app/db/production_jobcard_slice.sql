-- ===========================================================================
-- production_jobcard_slice.sql
-- ---------------------------------------------------------------------------
-- Schema-only DDL for the JOB-CARD + PRODUCTION slice of the Candor ERP, plus
-- the minimal supporting tables they have FOREIGN-KEY dependencies on. 47 tables.
--
-- Generated from app/db/test_db/supabase/supabase_schema_all_tables.sql by taking
-- the FK-dependency closure of every table named job_card* / production* /
-- jc_material_exception_v2 (41 tables) + the 6 hard FK parents they reference:
--   auth_user, auth_role, bom_header, bom_line, bom_amendment_request_v2, machine.
--
-- SCOPE CAVEAT: this is the DDL-clean closure, NOT the runtime-complete set. The
-- production/planning code QUERIES ~98 of the 170 tables via plain *_id columns
-- with no FK constraint (SO, inventory, PO, QC, floor stock, offgrade,
-- material_document, ...). Those are intentionally NOT here — the module will hit
-- missing tables at runtime. For a working module, load the full
-- supabase_schema_all_tables.sql instead.
--
-- Self-contained: includes the 3 trigger functions the tables' triggers call.
-- No custom TYPEs are used by these tables. Verified: restores into a fresh
-- PostgreSQL 16 database with 0 errors.
--
-- Run manually (percent-encode the password: @ -> %40, * -> %2A):
--   psql "postgresql://USER:ENC_PWD@HOST:5432/DB?sslmode=require" -f production_jobcard_slice.sql
-- ===========================================================================

SET statement_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SET check_function_bodies = false;
SET client_min_messages = warning;

-- ── trigger functions (must exist before the CREATE TRIGGER statements below) ──
CREATE OR REPLACE FUNCTION public.fn_sync_phase_batch_id()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
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
$function$
;

CREATE OR REPLACE FUNCTION public.fn_touch_updated_at()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$function$
;

CREATE OR REPLACE FUNCTION public.fn_touch_jcvar_last_updated()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
    NEW.last_updated_at := NOW();
    RETURN NEW;
END;
$function$
;


-- ── tables, sequences, constraints, indexes, triggers ──
--
-- PostgreSQL database dump
--


-- Dumped from database version 16.14 (Debian 16.14-1.pgdg13+1)
-- Dumped by pg_dump version 16.14 (Debian 16.14-1.pgdg13+1)

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

SET default_tablespace = '';

SET default_table_access_method = heap;

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
    CONSTRAINT production_plan_line_v2_shift_check CHECK (((shift)::text = ANY (ARRAY[('A'::character varying)::text, ('B'::character varying)::text, ('C'::character varying)::text, ('general'::character varying)::text]))),
    CONSTRAINT production_plan_line_v2_status_check CHECK (((status)::text = ANY (ARRAY[('planned'::character varying)::text, ('in_progress'::character varying)::text, ('completed'::character varying)::text, ('cancelled'::character varying)::text])))
);


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
    CONSTRAINT production_plan_v2_entity_check CHECK (((entity)::text = ANY (ARRAY[('cfpl'::character varying)::text, ('cdpl'::character varying)::text]))),
    CONSTRAINT production_plan_v2_plan_type_check CHECK (((plan_type)::text = ANY (ARRAY[('daily'::character varying)::text, ('weekly'::character varying)::text]))),
    CONSTRAINT production_plan_v2_revision_number_check CHECK ((revision_number >= 0)),
    CONSTRAINT production_plan_v2_status_check CHECK (((status)::text = ANY (ARRAY[('draft'::character varying)::text, ('approved'::character varying)::text, ('executed'::character varying)::text, ('cancelled'::character varying)::text]))),
    CONSTRAINT production_plan_v2_warehouse_check CHECK (((warehouse)::text = ANY (ARRAY[('W-202'::character varying)::text, ('A-185'::character varying)::text])))
);


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
-- Name: auth_role role_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_role ALTER COLUMN role_id SET DEFAULT nextval('public.auth_role_role_id_seq'::regclass);


--
-- Name: auth_user user_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_user ALTER COLUMN user_id SET DEFAULT nextval('public.auth_user_user_id_seq'::regclass);


--
-- Name: bom_header bom_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bom_header ALTER COLUMN bom_id SET DEFAULT nextval('public.bom_header_bom_id_seq'::regclass);


--
-- Name: bom_line bom_line_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bom_line ALTER COLUMN bom_line_id SET DEFAULT nextval('public.bom_line_bom_line_id_seq'::regclass);


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
-- Name: machine machine_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.machine ALTER COLUMN machine_id SET DEFAULT nextval('public.machine_machine_id_seq'::regclass);


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
-- Name: machine machine_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.machine
    ADD CONSTRAINT machine_pkey PRIMARY KEY (machine_id);


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
-- Name: idx_auth_user_phone; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_auth_user_phone ON public.auth_user USING btree (phone);


--
-- Name: idx_balance_mat_bom_line; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_balance_mat_bom_line ON public.job_card_balance_material USING btree (bom_line_id);


--
-- Name: idx_balance_mat_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_balance_mat_jc ON public.job_card_balance_material USING btree (job_card_id);


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
-- Name: idx_env_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_env_jc ON public.job_card_environment USING btree (job_card_id);


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
-- Name: idx_loss_recon_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_loss_recon_jc ON public.job_card_loss_reconciliation USING btree (job_card_id);


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
-- Name: idx_metal_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_metal_jc ON public.job_card_metal_detection USING btree (job_card_id);


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
-- Name: idx_prod_order_entity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_prod_order_entity ON public.production_order USING btree (entity);


--
-- Name: idx_prod_order_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_prod_order_status ON public.production_order USING btree (status);


--
-- Name: idx_remarks_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_remarks_jc ON public.job_card_remarks USING btree (job_card_id);


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
-- Name: idx_rm_v2_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_rm_v2_jc ON public.job_card_rm_indent_v2 USING btree (job_card_id);


--
-- Name: idx_sign_off_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sign_off_jc ON public.job_card_sign_off USING btree (job_card_id);


--
-- Name: idx_weight_jc; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_weight_jc ON public.job_card_weight_check USING btree (job_card_id);


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
-- Name: auth_user auth_user_role_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.auth_user
    ADD CONSTRAINT auth_user_role_id_fkey FOREIGN KEY (role_id) REFERENCES public.auth_role(role_id);


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
-- PostgreSQL database dump complete
--


