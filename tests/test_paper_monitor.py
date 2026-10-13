import importlib.util, io, json, sqlite3, sys, tempfile, unittest, urllib.error
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).parents[1] / "scripts" / "paper_monitor.py"
spec = importlib.util.spec_from_file_location("paper_monitor", SCRIPT)
pm = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pm
spec.loader.exec_module(pm)

class MonitorTests(unittest.TestCase):
    def test_partial_publication_dates_keep_precision_and_empty_updates_preserve_date(self):
        for parts, expected in [([2026], ("2026-01-01", "year")), ([2026, 10], ("2026-10-01", "month")),
                                ([2026, 10, 9], ("2026-10-09", "day"))]:
            self.assertEqual(pm.date_parts_with_precision({"published-print": {"date-parts": [parts]}}), expected)
        with tempfile.TemporaryDirectory() as td:
            db = pm.db_open(Path(td) / "papers.sqlite3")
            paper = pm.Paper("doi:10.1/date", "10.1/date", "Research title", "", "Journal", "2026-10-01", "", [], "Crossref",
                             published_precision="month")
            pm.upsert(db, paper, "before")
            paper.published = ""; paper.published_precision = "unknown"
            pm.upsert(db, paper, "after")
            restored = pm.row_to_paper(db.execute("SELECT * FROM papers").fetchone())
            self.assertEqual((restored.published, restored.published_precision), ("2026-10-01", "month"))
            paper.published = "2026-10-09"; paper.published_precision = "day"
            pm.upsert(db, paper, "latest")
            restored = pm.row_to_paper(db.execute("SELECT * FROM papers").fetchone())
            self.assertEqual((restored.published, restored.published_precision), ("2026-10-09", "day"))
            db.close()

    def test_openalex_paratext_cannot_overwrite_doi_deposit_and_crossref_can_repair_it(self):
        with tempfile.TemporaryDirectory() as td:
            db = pm.db_open(Path(td) / "papers.sqlite3")
            doi = "10.1287/mnsc.2024.05008"
            title = "Index-Based Yield Protection for Smallholder Farmers"
            registry = pm.Paper("doi:" + doi, doi, title, "", "Management Science", "2026-09-18", "", [], "Crossref",
                                publication_type_raw="journal-article", publication_type_source="crossref")
            pm.upsert(db, registry, "first")
            weak = pm.Paper("doi:" + doi, doi, title, "", "Management Science", "2026-09-18", "", [], "OpenAlex",
                            publication_type_raw="paratext", publication_type_source="openalex")
            pm.upsert(db, weak, "second")
            saved = db.execute("SELECT * FROM papers").fetchone()
            self.assertEqual((saved["eligibility_status"], saved["publication_type_raw"], saved["publication_type_source"]),
                             ("eligible", "journal-article", "crossref"))
            self.assertTrue(any(e.get("source") == "openalex" and e.get("value") == "paratext" for e in json.loads(saved["publication_type_evidence_json"])))
            db.execute("UPDATE papers SET publication_type_raw='paratext',publication_type_source='openalex',eligibility_status='excluded',publication_type='Front/Back Matter'")
            repaired = pm.Paper("doi:" + doi, doi, title, "", "Management Science", "2026-09-18", "", [], "Crossref",
                               publication_type_raw="journal-article", publication_type_source="crossref")
            pm.upsert(db, repaired, "third")
            saved = db.execute("SELECT * FROM papers").fetchone()
            self.assertEqual((saved["eligibility_status"], saved["needs_rescreen"]), ("eligible", 1))
            db.close()

    def test_generic_api_type_cannot_overwrite_official_comment(self):
        with tempfile.TemporaryDirectory() as td:
            db=pm.db_open(Path(td)/'papers.sqlite3')
            official=pm.Paper('doi:10.1/x','10.1/x','Discussion of AI','Evidence','Journal','2026-01-01','',[],'Official',publication_type_raw='Comment',publication_type_source='publisher-official')
            pm.upsert(db,official,'before')
            api=pm.Paper('doi:10.1/x','10.1/x','Discussion of AI','Longer abstract evidence','Journal','2026-01-01','',[],'OpenAlex',publication_type_raw='article',publication_type_source='openalex',source_kind='journal')
            pm.upsert(db,api,'after')
            saved=db.execute('SELECT * FROM papers').fetchone()
            self.assertEqual(saved['eligibility_status'],'excluded')
            self.assertEqual(saved['publication_type_source'],'publisher-official')
            self.assertEqual(saved['abstract'],'Longer abstract evidence')
            db.close()

    def test_collection_recovers_a_gap_longer_than_rolling_window(self):
        with tempfile.TemporaryDirectory() as td:
            db = pm.db_open(Path(td)/'papers.sqlite3')
            pm.update_source_health(db, 'V', 'healthy', '2020-01-01T00:00:00+00:00')
            with patch.object(pm, 'crossref_collect', return_value=[]) as collect:
                pm.collect_into_db({'sources':[{'name':'V','type':'crossref','issn':'1234'}]}, db, 'now', 'recovery')
            self.assertEqual(collect.call_args.args[2], '2019-12-31')
            db.close()

    @staticmethod
    def structured_result(identity, *, confidence=0.95):
        return {
            "identity": identity,
            "reasoning": {
                "evidence_summary": "摘要研究人在回路的偏好表达如何影响候选方案的迭代与最终选择过程",
                "profile_connection": "直接连接交互式优化与偏好融合这一核心研究主题",
                "transfer_value": "实验任务和过程指标可迁移到车辆路径决策支持研究",
                "limitations": "应用场景不同且尚需核对样本和外部效度",
            },
            "score_dimensions": {
                "core_relevance": 0.9, "mechanism_alignment": 0.9,
                "method_transfer": 0.8, "evidence_quality": 0.9,
                "boundary_penalty": 0.0,
            },
            "matched_themes": ["interactive optimization"],
            "confidence": confidence,
            "recommendation_reason": "摘要显示研究通过人在回路的偏好表达推进候选方案迭代；其价值在于把交互式优化机制转成可复用的实验任务和过程指标，可迁移到车辆路径决策支持，但应用场景与外部效度仍需进一步核验。",
            "recommendation_type": "core",
            "evidence_anchors": [{"source":"abstract","quote":"Abstract","claim":"原始摘要为本次判断提供了可追溯的内容证据"}],
        }

    def test_doi_normalization_and_identity(self):
        self.assertEqual(pm.normalize_doi("https://doi.org/10.1145/ABC. "), "10.1145/abc")
        self.assertEqual(pm.identity("10.1/X", "A"), pm.identity("doi:10.1/x", "B"))
        self.assertEqual(pm.identity("", "Human–AI  Systems"), pm.identity("", "Human AI Systems"))

    def test_clean_structured_abstract(self):
        raw = "<jats:p>Preference &amp; routing</jats:p>\n  test"
        self.assertEqual(pm.clean_text(raw), "Preference & routing test")

    def test_placeholder_abstract_cannot_block_real_enrichment(self):
        with tempfile.TemporaryDirectory() as td:
            db=pm.db_open(Path(td)/"x.sqlite3")
            paper=pm.Paper("doi:10.1/x","10.1/x","A","International audience","V","","",[],"s")
            pm.upsert(db,paper,"before")
            pending=pm.Paper("doi:10.1/x","10.1/x","A","","V","","",[],"s")
            with patch.object(pm,"openalex_abstract",return_value="A real original abstract with study evidence") as lookup:
                pm.enrich_missing_abstracts([pending],{},db)
            lookup.assert_called_once()
            self.assertEqual(pending.abstract,"A real original abstract with study evidence")
            self.assertIn("api.openalex.org",pending.abstract_source_url)
            pm.upsert(db,pending,"after")
            saved=pm.row_to_paper(db.execute("SELECT * FROM papers").fetchone())
            self.assertEqual(saved.abstract_source_url,pending.abstract_source_url)
            self.assertEqual(db.execute("SELECT abstract_retrieved_at FROM papers").fetchone()[0],"after")
            shorter=pm.Paper("doi:10.1/x","10.1/x","A","Short","V","","",[],"s",abstract_source_url="https://wrong.test")
            pm.upsert(db,shorter,"later")
            self.assertEqual(db.execute("SELECT abstract_source_url FROM papers").fetchone()[0],pending.abstract_source_url)
            db.close()

    def test_openalex_abstract_rejects_other_doi_and_placeholder(self):
        for doi,text in [("10.1/other","Original evidence"),("10.1/x","International audience")]:
            words={word:[index] for index,word in enumerate(text.split())}
            with patch.object(pm,"request_json",return_value={"doi":doi,"abstract_inverted_index":words}):
                self.assertEqual(pm.openalex_abstract("10.1/x",{}),"")

    def test_duplicate_collection_preserves_original_abstract_source(self):
        first=pm.Paper("doi:10.1/x","10.1/x","A","Original evidence","V","","",[],"s",abstract_source="crossref",abstract_source_url="https://api.crossref.org/works/x")
        second=pm.Paper("doi:10.1/x","10.1/x","A","","V","","",[],"s")
        pm.enrich_missing_abstracts([first,second],{})
        self.assertEqual(second.abstract_source,"crossref")
        self.assertEqual(second.abstract_source_url,first.abstract_source_url)

    def test_crossref_parsing_and_chi_filter(self):
        fixture = json.loads((Path(__file__).parent/"fixtures"/"crossref.json").read_text())
        cfg={"collection":{},"user_agent":"test"}
        with patch.object(pm,"request_json",return_value=fixture):
            papers=pm.crossref_collect({"name":"CHI","type":"crossref-query","query_container":"CHI Conference"},cfg,"2026-01-01")
        self.assertEqual(len(papers),1)
        self.assertEqual(papers[0].doi,"10.1145/3706598.3710001")
        self.assertIn("Human-AI",papers[0].title)

    def test_crossref_cursor_pagination_and_deduplication(self):
        def page(doi,title,next_cursor):
            return {"message":{"items":[{"DOI":doi,"title":[title],"container-title":["Venue"]}],
                               "next-cursor":next_cursor}}
        responses=[page("10.1/a","A","next"),page("10.1/b","B",None)]
        cfg={"collection":{"rows_per_page":1,"max_pages_per_source":3},"user_agent":"test"}
        with patch.object(pm,"request_json",side_effect=responses) as request:
            papers=pm.crossref_collect({"name":"V","type":"crossref","issn":"1234"},cfg,"2026-01-01")
        self.assertEqual([p.doi for p in papers],["10.1/a","10.1/b"])
        self.assertEqual(request.call_count,2)
        self.assertIn("cursor=next",request.call_args_list[1].args[0])

    def test_truncated_collection_keeps_records_and_complete_checkpoint(self):
        with tempfile.TemporaryDirectory() as td:
            db=pm.db_open(Path(td)/"x.sqlite3")
            pm.update_source_health(db,"V","healthy","2020-01-01T00:00:00+00:00")
            payload={"message":{"items":[{"DOI":"10.1/a","title":["A"],"abstract":"Evidence"}],
                                "next-cursor":"more","total-results":20}}
            cfg={"collection":{"rows_per_page":1,"max_pages_per_source":1,"openalex_fallback":False},
                 "sources":[{"name":"V","type":"crossref","issn":"1234"}]}
            with patch.object(pm,"request_json",return_value=payload):
                collected,new,failures=pm.collect_into_db(cfg,db,"2026-10-09T00:00:00+00:00","run")
            self.assertEqual((len(collected),len(new)),(1,1))
            self.assertEqual(failures[0]["status"],"degraded")
            self.assertIn("page limit",failures[0]["error"])
            self.assertEqual(db.execute("SELECT last_success_at FROM source_health").fetchone()[0],"2020-01-01T00:00:00+00:00")
            db.close()

    def test_disabled_or_unconfigured_source_cannot_be_healthy(self):
        with tempfile.TemporaryDirectory() as td:
            db=pm.db_open(Path(td)/"x.sqlite3")
            _,_,failures=pm.collect_into_db({"sources":[{"name":"V","type":"openalex"}]},db,"now","run")
            self.assertEqual(failures[0]["status"],"failed")
            self.assertEqual(db.execute("SELECT status FROM source_health").fetchone()[0],"failed")
            db.close()

    def test_primary_openalex_remains_enabled_when_fallback_is_disabled(self):
        with tempfile.TemporaryDirectory() as td:
            db=pm.db_open(Path(td)/"x.sqlite3")
            cfg={"collection":{"openalex_fallback":False},"sources":[{"name":"V","type":"openalex","openalex_id":"S1"}]}
            with patch.object(pm,"openalex_collect",return_value=[]) as collect:
                _,_,failures=pm.collect_into_db(cfg,db,"now","run")
            collect.assert_called_once()
            self.assertEqual(failures,[])
            db.close()

    def test_invalid_provider_payload_is_a_failure(self):
        with patch.object(pm,"request_json",return_value={"message":{}}):
            with self.assertRaises(pm.CollectionIncomplete):
                pm.crossref_collect({"name":"V","type":"crossref","issn":"1234"},{},"2026-01-01")

    def test_source_specific_limits_can_cover_large_conference_query(self):
        source={"name":"V","type":"crossref","issn":"1234","rows_per_page":1000,"max_pages_per_source":5}
        with patch.object(pm,"request_json",return_value={"message":{"items":[]}}) as request:
            pm.crossref_collect(source,{"collection":{"rows_per_page":80}},"2026-01-01")
        self.assertIn("rows=1000",request.call_args.args[0])

    def test_retry_honors_retry_after_then_succeeds(self):
        error=urllib.error.HTTPError("https://example.test",429,"limited",{"Retry-After":"0"},io.BytesIO())
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): return False
            def read(self): return b'{"ok": true}'
        with patch.object(pm.urllib.request,"urlopen",side_effect=[error,Response()]), patch.object(pm.time,"sleep") as sleep:
            result=pm.request_json("https://example.test",{},1,1,0.1)
        self.assertTrue(result["ok"]); sleep.assert_called_once_with(0.0)

    def test_openalex_survives_crossref_failure_and_marks_degraded(self):
        with tempfile.TemporaryDirectory() as td:
            db=pm.db_open(Path(td)/"x.sqlite3")
            paper=pm.Paper("doi:10.1/x","10.1/x","A","Abstract","V","2026-01-01","u",[],"V / OpenAlex")
            cfg={"lookback_days":1,"collection":{"openalex_fallback":True},"sources":[
                {"name":"V","type":"crossref","issn":"1234","openalex_id":"S1"}]}
            with patch.object(pm,"crossref_collect",side_effect=RuntimeError("down")), \
                 patch.object(pm,"openalex_collect",return_value=[paper]):
                collected,new,failures=pm.collect_into_db(cfg,db,"now","run")
            self.assertEqual((len(collected),len(new)),(1,1))
            self.assertEqual(failures[0]["status"],"degraded")
            health=db.execute("select status,consecutive_failures from source_health where source='V'").fetchone()
            self.assertEqual(tuple(health),("degraded",0))

    def test_normal_agent_export_enriches_before_freezing_single_queue(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/"research-profile.md").write_text("profile",encoding="utf-8")
            config=root/"config.toml"
            config.write_text('state_dir = "."\nprofile_file = "research-profile.md"\n[[sources]]\nname = "A"\ntype = "crossref"\nissn = "1234"\n',encoding="utf-8")
            paper=pm.Paper("doi:10.1/x","10.1/x","A","","V","2026-01-01","u",[],"s",
                           publication_type_raw="journal-article",publication_type_source="crossref")
            def collected(cfg,db,now,run_id):
                pm.upsert(db,paper,now); db.commit(); return [paper],[paper],[]
            def enriched(path,cfg,limit=500):
                db=pm.db_open(path)
                with db: db.execute("UPDATE papers SET abstract='Recovered',needs_rescreen=1")
                db.close(); return {"updated":1,"unresolved":0,"status":"succeeded"}
            with patch.object(pm,"collect_into_db",side_effect=collected), patch.object(pm,"run_enrichment",side_effect=enriched), patch("builtins.print") as output:
                pm.agent_export(config)
            summary=json.loads(output.call_args.args[0]); queue=json.loads(Path(summary["queue_path"]).read_text())
            self.assertEqual(summary["enrichment"]["updated"],1)
            self.assertEqual(queue["papers"][0]["abstract"],"Recovered")
            db=pm.db_open(root/"papers.sqlite3")
            self.assertIsNotNone(db.execute("SELECT 1 FROM run_papers WHERE run_id=? AND role='new'",(summary["run_id"],)).fetchone())
            db.close()

    def test_collection_batch_carries_api_and_official_new_papers_into_one_queue(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/"research-profile.md").write_text("profile",encoding="utf-8")
            config=root/"config.toml"
            config.write_text('state_dir = "."\nprofile_file = "research-profile.md"\n[[sources]]\nname = "A"\ntype = "crossref"\nissn = "1234"\n',encoding="utf-8")
            api=pm.Paper("doi:10.1/api","10.1/api","API paper","Abstract","A","2026-01-01","u",[],"A",
                         publication_type_raw="journal-article",publication_type_source="crossref")
            def collected(cfg,db,now,run_id):
                pm.upsert(db,api,now); db.commit(); return [api],[api],[]
            with patch.object(pm,"collect_into_db",side_effect=collected), patch("builtins.print") as output:
                self.assertEqual(pm.collect_only(config),0)
            batch=json.loads(output.call_args.args[0])
            db=pm.db_open(root/"papers.sqlite3")
            official=pm.Paper("doi:10.1/official","10.1/official","Official paper","Publisher abstract","A",
                              "2026-01-02","u",[],"A / Official issue 1",
                              publication_type_raw="journal-article",publication_type_source="publisher-official")
            pm.upsert(db,official,"9999-01-01T00:00:00+00:00"); db.commit(); db.close()
            with patch("builtins.print") as output:
                self.assertEqual(pm.agent_export(config,no_collect=True,batch_run=batch["run_id"]),0)
            summary=json.loads(output.call_args.args[0]); queue=json.loads(Path(summary["queue_path"]).read_text())
            self.assertEqual(queue["collection_run_id"],batch["run_id"])
            self.assertEqual({item["identity"] for item in queue["papers"]},{api.identity,official.identity})
            self.assertEqual(summary["new"],2)

    def test_duplicate_provider_records_share_one_abstract_lookup(self):
        a=pm.Paper("doi:10.1/x","10.1/x","A","","V","2026-01-01","u",[],"crossref")
        b=pm.Paper("doi:10.1/x","10.1/x","A","","V","2026-01-01","u",[],"openalex")
        with patch.object(pm,"openalex_abstract",return_value="Shared abstract") as lookup:
            pm.enrich_missing_abstracts([a,b],{"collection":{"openalex_fallback":True}})
        lookup.assert_called_once(); self.assertEqual((a.abstract,b.abstract),("Shared abstract","Shared abstract"))

    def test_existing_database_abstract_avoids_network_lookup(self):
        with tempfile.TemporaryDirectory() as td:
            db=pm.db_open(Path(td)/"x.sqlite3")
            stored=pm.Paper("doi:10.1/x","10.1/x","A","Stored abstract","V","2026-01-01","u",[],"old")
            pm.upsert(db,stored,"now"); db.commit()
            incoming=pm.Paper("doi:10.1/x","10.1/x","A","","V","2026-01-01","u",[],"new")
            with patch.object(pm,"openalex_abstract") as lookup:
                pm.enrich_missing_abstracts([incoming],{},db)
            lookup.assert_not_called(); self.assertEqual(incoming.abstract,"Stored abstract")

    def test_upsert_is_idempotent_and_enriches_abstract(self):
        with tempfile.TemporaryDirectory() as td:
            db=pm.db_open(Path(td)/"x.sqlite3")
            p=pm.Paper(pm.identity("10.1/x","A"),"10.1/x","A","","V","2026-01-01","u",[],"s1")
            self.assertTrue(pm.upsert(db,p,"t1")); db.commit()
            p.abstract="A longer abstract"; p.source="s2"
            self.assertFalse(pm.upsert(db,p,"t2")); db.commit()
            self.assertEqual(db.execute("select count(*) from papers").fetchone()[0],1)
            self.assertEqual(db.execute("select abstract from papers").fetchone()[0],"A longer abstract")
            self.assertEqual(db.execute("select abstract_source from papers").fetchone()[0],"metadata")
            self.assertEqual(db.execute("select count(*) from observations").fetchone()[0],2)

    def test_provider_upsert_reuses_title_only_manual_identity(self):
        with tempfile.TemporaryDirectory() as td:
            db=pm.db_open(Path(td)/"x.sqlite3")
            manual_identity=pm.identity("","Preference-aware route planning")
            db.execute("""INSERT INTO papers(
              identity,title,abstract,venue,authors_json,first_seen,updated_at,manual_citation,
              eligibility_status,needs_rescreen
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
              (manual_identity,"Preference-aware route planning","User abstract","Transportation Science",
               "[]","t0","t0","Smith (2026). Preference-aware route planning. Transportation Science.",
               "eligible",1))
            db.commit()
            incoming=pm.Paper("doi:10.1287/trsc.2026.9999","10.1287/trsc.2026.9999",
              "Preference-aware route planning","Publisher abstract","Transportation Science","2026","u",[],"provider",
              publication_type_raw="journal-article",publication_type_source="crossref")
            self.assertFalse(pm.upsert(db,incoming,"t1")); db.commit()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM papers").fetchone()[0],1)
            saved=db.execute("SELECT identity,doi FROM papers").fetchone()
            self.assertEqual(tuple(saved),(manual_identity,"10.1287/trsc.2026.9999"))

    def test_agent_export_skips_low_priority_papers_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/"research-profile.md").write_text("profile",encoding="utf-8")
            config=root/"config.toml"
            config.write_text('state_dir = "."\nprofile_file = "research-profile.md"\n[[sources]]\nname = "A"\ntype = "crossref"\nissn = "1234"\n',encoding="utf-8")
            db=pm.db_open(root/"papers.sqlite3")
            pm.upsert(db,pm.Paper("doi:10.1/x","10.1/x","Editorial Board","","V","2026-01-01","u",[],"s"),"now")
            db.commit(); db.close()
            with patch("builtins.print") as output:
                pm.agent_export(config,no_collect=True)
            summary=json.loads(output.call_args.args[0])
            queue=json.loads(Path(summary["queue_path"]).read_text())
            self.assertEqual(queue["papers"],[])

    def test_enriched_abstract_requires_explicit_rescreen_and_exports_publication_type(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); profile="profile"; (root/"research-profile.md").write_text(profile,encoding="utf-8")
            config=root/"config.toml"
            config.write_text('state_dir = "."\nprofile_file = "research-profile.md"\n[[sources]]\nname = "A"\ntype = "crossref"\nissn = "1234"\n',encoding="utf-8")
            db=pm.db_open(root/"papers.sqlite3")
            paper=pm.Paper("doi:10.1/x","10.1/x","A","Recovered abstract","V","2026-01-01","u",[],"s",
                           publication_type_raw="journal-article",publication_type_source="crossref")
            pm.upsert(db,paper,"now")
            profile_hash=__import__("hashlib").sha256(profile.encode()).hexdigest()
            db.execute("""INSERT INTO screenings(identity,profile_hash,provider,model,relevant,score,screened_at)
                        VALUES(?,?,'codex-agent','old',0,0.1,'before')""",(paper.identity,profile_hash))
            db.execute("UPDATE papers SET needs_rescreen=1 WHERE identity=?",(paper.identity,))
            db.commit(); db.close()
            with patch("builtins.print") as output: pm.agent_export(config,no_collect=True)
            queue=json.loads(Path(json.loads(output.call_args.args[0])["queue_path"]).read_text())
            self.assertEqual(queue["papers"],[])
            with patch("builtins.print") as output: pm.agent_export(config,rescreen=True,no_collect=True)
            queue=json.loads(Path(json.loads(output.call_args.args[0])["queue_path"]).read_text())
            self.assertEqual([item["identity"] for item in queue["papers"]],[paper.identity])
            self.assertEqual(queue["papers"][0]["publication_type"],"Journal Article")

    def test_direct_model_provider_paths_are_removed(self):
        self.assertFalse(hasattr(pm,"llm_screen"))
        self.assertFalse(hasattr(pm,"heuristic_screen"))
        self.assertFalse(hasattr(pm,"run"))

    def test_model_json_validation(self):
        out=pm.extract_json('```json\n{"relevant":true,"score":2,"reasons":"x","matched_themes":["a"],"confidence":-1}\n```')
        self.assertEqual(out["score"],1); self.assertEqual(out["confidence"],0)
        with self.assertRaises(ValueError): pm.extract_json('{"relevant":true}')

    def test_enrich_abstracts_updates_stored_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "research-profile.md").write_text("profile", encoding="utf-8")
            config = root / "config.toml"
            config.write_text(
                'state_dir = "."\nprofile_file = "research-profile.md"\n'
                '[[sources]]\nname = "A"\ntype = "crossref"\nissn = "1234"\n',
                encoding="utf-8",
            )
            db = pm.db_open(root / "papers.sqlite3")
            paper=pm.Paper("doi:10.1/x", "10.1/x", "A", "", "V", "", "", [], "s")
            paper.publication_type_raw="journal-article"; paper.publication_type_source="crossref"
            pm.upsert(db,paper,"now")
            db.commit()
            db.close()
            def found(db,paper,client):
                return {"abstract":"Recovered abstract","source_name":"crossref","source_url":"https://api.crossref.org/v1/works/10.1/x",
                        "evidence_type":"crossref_metadata","publication_type_raw":"journal-article","publication_type_source":"crossref"}
            with patch("academic_radar.enrichment.PROVIDERS",[("crossref",found)]), patch("builtins.print"):
                self.assertEqual(pm.enrich_abstracts(config), 0)
            db = pm.db_open(root / "papers.sqlite3")
            row = db.execute("SELECT abstract,abstract_source FROM papers").fetchone()
            self.assertEqual(tuple(row), ("Recovered abstract", "crossref"))
            db.close()

    def test_python39_toml_fallback(self):
        text='state_dir = "~/x"\nflag = true\n[s]\nn = 3\n[[sources]]\nname = "A"\ntype = "crossref"\n'
        out=pm._toml_load_fallback(text)
        self.assertEqual(out["s"]["n"],3); self.assertEqual(out["sources"][0]["name"],"A")

    def test_import_preserves_review_needed_when_feedback_or_evidence_changed_after_export(self):
        for change, expected in [("feedback", 1), ("late_feedback", 1), ("abstract", 1), ("reading", 0)]:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as td:
                root=Path(td); config=root/"config.toml"; database=root/"papers.sqlite3"
                (root/"research-profile.md").write_text("profile")
                config.write_text('state_dir = "."\nprofile_file = "research-profile.md"\n[[sources]]\nname = "V"\ntype = "crossref"\nissn = "1234"\n')
                db=pm.db_open(database)
                abstract="Abstract evidence provided for the complete original design."
                paper=pm.Paper("doi:10.1/update","10.1/update","Preference-aware routing",abstract,"V","2026-01-01","",[],"s",
                               publication_type_raw="journal-article",publication_type_source="crossref")
                pm.upsert(db,paper,"before"); db.commit(); db.close()
                with patch("builtins.print") as output:pm.agent_export(config,no_collect=True)
                summary=json.loads(output.call_args.args[0]);queue=json.loads(Path(summary["queue_path"]).read_text())
                db=pm.db_open(database)
                with db:
                    if change in {"feedback", "late_feedback"}:
                        stamp = "2020-01-01T00:00:00Z" if change == "late_feedback" else "9999-01-01T00:00:00Z"
                        db.execute("INSERT INTO feedback_events(identity,interest,reason,favorite,reading_status,created_at) VALUES(?,'not_interested','A new confirmed preference',0,'unread',?)",(paper.identity,stamp))
                        db.execute("UPDATE papers SET needs_rescreen=1")
                    elif change=="abstract":db.execute("UPDATE papers SET abstract=abstract||' Additional design details.',needs_rescreen=1")
                    else:db.execute("INSERT INTO paper_feedback(identity,favorite,reading_status,created_at,updated_at) VALUES(?,1,'read','now','now')",(paper.identity,))
                db.close()
                result=self.structured_result(paper.identity);result["evidence_anchors"][0]["quote"]=abstract
                path=root/"results.json";path.write_text(json.dumps({"run_id":summary["run_id"],"profile_hash":queue["profile_hash"],"model":"codex-test","results":[result]}))
                with patch("builtins.print"):self.assertEqual(pm.agent_import(config,path),0)
                db=pm.db_open(database)
                self.assertEqual(db.execute("SELECT needs_rescreen FROM papers").fetchone()[0],expected)
                self.assertEqual(db.execute("SELECT status FROM agent_jobs").fetchone()[0],"imported")
                db.close()

    def test_agent_import_records_semantic_judgment(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/"research-profile.md").write_text("interactive optimization",encoding="utf-8")
            (root/"config.toml").write_text('state_dir = "."\nprofile_file = "research-profile.md"\nrelevance_threshold = 0.62\n[[sources]]\nname = "A"\ntype = "crossref"\nissn = "1234-5678"\n',encoding="utf-8")
            db=pm.db_open(root/"papers.sqlite3")
            p=pm.Paper("doi:10.1/x","10.1/x","Preference-aware routing","Abstract","V","2026-01-01","u",[],"s")
            p.publication_type_raw="journal-article"; p.publication_type_source="crossref"
            pm.upsert(db,p,"now"); db.commit(); db.close()
            profile_hash=__import__("hashlib").sha256(b"interactive optimization").hexdigest()
            with patch("builtins.print") as output:
                pm.agent_export(root/"config.toml",no_collect=True)
            run_id=json.loads(output.call_args.args[0])["run_id"]
            results={"run_id":run_id,"profile_hash":profile_hash,"model":"codex-test","results":[
              self.structured_result(p.identity)]}
            path=root/"results.json"; path.write_text(json.dumps(results),encoding="utf-8")
            self.assertEqual(pm.agent_import(root/"config.toml",path),0)
            db=pm.db_open(root/"papers.sqlite3")
            row=db.execute("select provider,relevant,score,reasons,rubric_version from screenings").fetchone()
            self.assertEqual((row[0],row[1],row[2]),("codex-agent",1,0.88))
            self.assertIn("人在回路",row[3])
            self.assertNotIn("论文证据：",row[3])
            self.assertEqual(row[4],"evidence-v3")
            saved=dict(db.execute("SELECT * FROM recommendation_snapshots").fetchone())
            self.assertEqual((saved['run_id'],saved['score'],saved['reasons']),(run_id,row[2],row[3]))
            with db:
                db.execute("UPDATE screenings SET score=.1,reasons='Later judgment'")
                db.execute("UPDATE recommendation_snapshots SET score=NULL,reasons=NULL,evidence_source='unavailable'")
            db.close()
            # A mismatched file must never be used to invent a historical judgment.
            results['profile_hash']='wrong-profile'
            path.write_text(json.dumps(results),encoding='utf-8')
            with patch('builtins.print') as output:
                pm.backfill_history(root/'config.toml')
            self.assertEqual(json.loads(output.call_args.args[0]),{'recovered':0,'unavailable':1})
            results['profile_hash']=profile_hash
            path.write_text(json.dumps(results),encoding='utf-8')
            with patch('builtins.print') as output:
                pm.backfill_history(root/'config.toml')
            self.assertEqual(json.loads(output.call_args.args[0]),{'recovered':1,'unavailable':0})
            with patch('builtins.print') as output:
                pm.backfill_history(root/'config.toml')
            self.assertEqual(json.loads(output.call_args.args[0])['recovered'],0)
            db=pm.db_open(root/'papers.sqlite3')
            restored=db.execute('SELECT score,reasons FROM recommendation_snapshots').fetchone()
            self.assertEqual(tuple(restored),(saved['score'],saved['reasons']))
            self.assertEqual(db.execute('SELECT score FROM screenings').fetchone()[0],.1)
            # Imports from before the selection ledger existed remain recoverable.
            with db:
                db.execute('DELETE FROM recommendation_snapshots')
                db.execute('DELETE FROM run_papers')
            db.close()
            with patch('builtins.print') as output:
                pm.backfill_history(root/'config.toml')
            self.assertEqual(json.loads(output.call_args.args[0]),{'recovered':1,'unavailable':0})

    def test_daily_export_skips_judged_papers_despite_feedback_profile_or_rubric_changes(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/"research-profile.md").write_text("current profile")
            config=root/"config.toml"
            config.write_text('state_dir = "."\nprofile_file = "research-profile.md"\n[[sources]]\nname = "V"\ntype = "crossref"\nissn = "1234"\n')
            db=pm.db_open(root/"papers.sqlite3")
            current_hash=__import__("hashlib").sha256(b"current profile").hexdigest()
            variants=[("feedback",current_hash,pm.SCREENING_RUBRIC_VERSION,1),
                      ("old-profile","previous-profile",pm.SCREENING_RUBRIC_VERSION,0),
                      ("old-rubric",current_hash,"evidence-v2",0),
                      ("never-judged",None,None,1)]
            for suffix,profile_hash,rubric,dirty in variants:
                paper=pm.Paper(f"doi:10.1/{suffix}",f"10.1/{suffix}",suffix,"Abstract","V","2026-01-01","",[],"s",
                               publication_type_raw="journal-article",publication_type_source="crossref")
                pm.upsert(db,paper,"now")
                db.execute("UPDATE papers SET needs_rescreen=? WHERE identity=?",(dirty,paper.identity))
                if profile_hash:
                    db.execute("""INSERT INTO screenings(identity,profile_hash,provider,model,relevant,score,
                      screened_at,rubric_version) VALUES(?,?,'codex-agent','previous',1,.8,'before',?)""",
                      (paper.identity,profile_hash,rubric))
            db.execute("INSERT INTO paper_feedback VALUES(?,?,?,?,?,?,?)",
                       ("doi:10.1/feedback","not_interested","Updated preference",0,"unread","now","now"))
            before=[tuple(row) for row in db.execute("SELECT * FROM screenings ORDER BY identity")]
            db.commit(); db.close()
            with patch("builtins.print") as output: pm.agent_export(config,no_collect=True)
            summary=json.loads(output.call_args.args[0]); queue=json.loads(Path(summary["queue_path"]).read_text())
            self.assertEqual([item["identity"] for item in queue["papers"]],["doi:10.1/never-judged"])
            self.assertEqual(summary["total_candidates"],1)
            self.assertEqual(queue["feedback_examples"][0]["reason"],"Updated preference")
            db=pm.db_open(root/"papers.sqlite3")
            self.assertEqual([tuple(row) for row in db.execute("SELECT * FROM screenings ORDER BY identity")],before)
            db.close()

    def test_unjudged_batches_resume_after_import_without_rejudging_on_sync_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/"research-profile.md").write_text("profile")
            config=root/"config.toml"
            config.write_text('state_dir = "."\nprofile_file = "research-profile.md"\nmax_candidates = 1\n[[sources]]\nname = "V"\ntype = "crossref"\nissn = "1234"\n')
            db=pm.db_open(root/"papers.sqlite3")
            phash=__import__("hashlib").sha256(b"profile").hexdigest()
            for suffix in ("a","b"):
                paper=pm.Paper(f"doi:10.1/{suffix}",f"10.1/{suffix}",suffix,"Abstract","V","2026-01-01","",[],"s",
                               publication_type_raw="journal-article",publication_type_source="crossref")
                pm.upsert(db,paper,"now")
            db.commit(); db.close()
            with patch("builtins.print") as output:pm.agent_export(config,no_collect=True)
            summary=json.loads(output.call_args.args[0]);queue=json.loads(Path(summary["queue_path"]).read_text())
            self.assertEqual((summary["candidates"],summary["deferred_candidates"]),(1,1))
            self.assertEqual(queue["papers"][0]["identity"],"doi:10.1/a")
            result_path=root/"results.json"
            result_path.write_text(json.dumps({"run_id":queue["run_id"],"profile_hash":phash,"model":"test","results":[self.structured_result("doi:10.1/a")]}))
            with patch("academic_radar.cloud_sync.sync_configured",side_effect=RuntimeError("Cloud unavailable")),patch("builtins.print") as output:
                self.assertEqual(pm.agent_import(config,result_path),1)
            self.assertEqual(json.loads(output.call_args.args[0])["cloud_sync"]["status"],"failed")
            db=pm.db_open(root/"papers.sqlite3")
            self.assertEqual(db.execute("SELECT status FROM agent_jobs WHERE run_id=?",(queue["run_id"],)).fetchone()[0],"imported")
            db.close()
            with patch("builtins.print") as output:pm.agent_export(config,no_collect=True)
            summary=json.loads(output.call_args.args[0]);queue=json.loads(Path(summary["queue_path"]).read_text())
            self.assertEqual(queue["papers"][0]["identity"],"doi:10.1/b")
            self.assertEqual(summary["deferred_candidates"],0)
            result_path.write_text(json.dumps({"run_id":queue["run_id"],"profile_hash":phash,"model":"test","results":[self.structured_result("doi:10.1/b")]}))
            with patch("academic_radar.cloud_sync.sync_configured",return_value={"status":"succeeded"}),patch("builtins.print"):
                self.assertEqual(pm.agent_import(config,result_path),0)
            with patch("builtins.print") as output: pm.agent_export(config,no_collect=True)
            summary=json.loads(output.call_args.args[0]);queue=json.loads(Path(summary["queue_path"]).read_text())
            self.assertEqual((queue["papers"],summary["total_candidates"],summary["deferred_candidates"]),([],0,0))

    def test_empty_daily_import_refreshes_status_and_syncs_without_changing_saved_judgments(self):
        from academic_radar.operations import recommendation_freshness
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/"research-profile.md").write_text("profile")
            config=root/"config.toml"
            config.write_text('state_dir = "."\nprofile_file = "research-profile.md"\n[[sources]]\nname = "V"\ntype = "crossref"\nissn = "1234"\n')
            db=pm.db_open(root/"papers.sqlite3")
            paper=pm.Paper("doi:10.1/old","10.1/old","Old paper","Abstract","V","2026-01-01","",[],"s",
                           publication_type_raw="journal-article",publication_type_source="crossref")
            pm.upsert(db,paper,"before"); db.commit(); db.close()
            with patch("builtins.print") as output: pm.agent_export(config,no_collect=True)
            queue=json.loads(Path(json.loads(output.call_args.args[0])["queue_path"]).read_text())
            result_path=root/"results.json"
            result_path.write_text(json.dumps({"run_id":queue["run_id"],"profile_hash":queue["profile_hash"],
                                              "model":"test","results":[self.structured_result(paper.identity)]}))
            with patch("academic_radar.cloud_sync.sync_configured",return_value={"status":"succeeded"}),patch("builtins.print"):
                self.assertEqual(pm.agent_import(config,result_path),0)
            db=pm.db_open(root/"papers.sqlite3")
            with db:
                db.execute("UPDATE agent_jobs SET imported_at='2020-01-01T00:00:00+00:00'")
            saved_judgments=[tuple(row) for row in db.execute("SELECT * FROM screenings")]
            saved_history=[tuple(row) for row in db.execute("SELECT * FROM recommendation_snapshots")]
            self.assertTrue(recommendation_freshness(db)["overdue"])
            db.close()
            with patch("builtins.print") as output: pm.agent_export(config,no_collect=True)
            queue=json.loads(Path(json.loads(output.call_args.args[0])["queue_path"]).read_text())
            self.assertEqual(queue["papers"],[])
            result_path.write_text(json.dumps({"run_id":queue["run_id"],"profile_hash":queue["profile_hash"],
                                              "model":"test","results":[],"source_failures":queue["source_failures"]}))
            with patch("academic_radar.cloud_sync.sync_configured",return_value={"status":"succeeded"}) as sync,patch("builtins.print") as output:
                self.assertEqual(pm.agent_import(config,result_path),0)
            sync.assert_called_once_with(config)
            summary=json.loads(output.call_args.args[0])
            self.assertEqual((summary["imported"],summary["relevant"]),(0,0))
            self.assertTrue(Path(summary["digest_path"]).exists())
            db=pm.db_open(root/"papers.sqlite3")
            self.assertEqual(tuple(db.execute("SELECT status,imported_count FROM agent_jobs WHERE run_id=?",(queue["run_id"],)).fetchone()),("imported",0))
            self.assertEqual(tuple(db.execute("SELECT status,relevant_count FROM pipeline_runs WHERE run_id=?",(queue["run_id"],)).fetchone()),("succeeded",0))
            self.assertTrue(recommendation_freshness(db)["is_today"])
            self.assertFalse(recommendation_freshness(db)["overdue"])
            self.assertEqual([tuple(row) for row in db.execute("SELECT * FROM screenings")],saved_judgments)
            self.assertEqual([tuple(row) for row in db.execute("SELECT * FROM recommendation_snapshots")],saved_history)
            db.close()

    def test_schema_four_does_not_get_mislabeled_as_new_rubric(self):
        paper={"abstract":"Abstract","title":"Paper"}
        item=self.structured_result("doi:10.1/x");item.pop("evidence_anchors");item.pop("recommendation_type")
        result=pm.structured_judgment(item,paper,.7,4)
        self.assertEqual(result["rubric_version"],"evidence-v2")

    def test_study_narrative_persists_without_changing_rubric_or_score(self):
        item=self.structured_result("doi:10.1/x")
        paper={"abstract":"Abstract","title":"Paper"}
        original=pm.structured_judgment(item,paper,.7,5)
        summary={
            "problem_motivation":"研究检验明确偏好能否进入决策机制，回应固定建议难以调整的问题。",
            "approach":"作者比较偏好输入条件下的决策过程，原摘要不足以确认详细实验规模。",
            "findings_value":"当前证据支持研究方向的具体关联，但没有完整结果，不能据此声称用户收益已获验证。",
        }
        item.update(study_summary=summary,recommendation_reason="\n\n".join(summary.values()))
        result=pm.structured_judgment(item,paper,.7,5)
        self.assertEqual(result["reasoning"]["study_summary"],summary)
        self.assertEqual(result["score"],original["score"])
        self.assertEqual(result["score_dimensions"],original["score_dimensions"])
        self.assertEqual(result["rubric_version"],"evidence-v3")

    def test_schema_five_rejects_nonfinite_scores(self):
        item=self.structured_result("doi:10.1/x");item["score_dimensions"]["core_relevance"]=float("nan")
        with self.assertRaisesRegex(ValueError,"finite"):
            pm.structured_judgment(item,{"abstract":"Abstract","title":"Paper"},.7,5)

    def test_agent_import_reports_all_shallow_results_without_writing_digest(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/"research-profile.md").write_text("profile",encoding="utf-8")
            config=root/"config.toml"
            config.write_text('state_dir = "."\nprofile_file = "research-profile.md"\n[[sources]]\nname = "A"\ntype = "crossref"\nissn = "1234"\n',encoding="utf-8")
            db=pm.db_open(root/"papers.sqlite3")
            identities=[]
            for suffix in ("a","b"):
                paper=pm.Paper(f"doi:10.1/{suffix}",f"10.1/{suffix}",suffix,"Abstract evidence", "V","2026-01-01","u",[],"s")
                paper.publication_type_raw="journal-article"; paper.publication_type_source="crossref"
                pm.upsert(db,paper,"now"); identities.append(paper.identity)
            db.commit(); db.close()
            with patch("builtins.print") as output: pm.agent_export(config,no_collect=True)
            summary=json.loads(output.call_args.args[0])
            first=self.structured_result(identities[0]); first.pop("recommendation_reason")
            second=self.structured_result(identities[1]); second["reasoning"]["limitations"]="场景不同。"
            path=root/"invalid.json"
            path.write_text(json.dumps({"run_id":summary["run_id"],"profile_hash":json.loads(Path(summary["queue_path"]).read_text())["profile_hash"],
                                        "model":"codex-test","results":[first,second]}),encoding="utf-8")
            with self.assertRaises(ValueError) as error: pm.agent_import(config,path)
            message=str(error.exception)
            self.assertIn(identities[0],message); self.assertIn("recommendation_reason",message)
            self.assertIn(identities[1],message); self.assertIn("limitations<18",message)
            db=pm.db_open(root/"papers.sqlite3")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM screenings").fetchone()[0],0)
            db.close()
            self.assertFalse(any((root/"digests").glob("*-agent.md")))

    def test_schema_three_queue_keeps_legacy_reason_compatibility(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/"research-profile.md").write_text("profile",encoding="utf-8")
            config=root/"config.toml"
            config.write_text('state_dir = "."\nprofile_file = "research-profile.md"\n[[sources]]\nname = "A"\ntype = "crossref"\nissn = "1234"\n',encoding="utf-8")
            db=pm.db_open(root/"papers.sqlite3")
            paper=pm.Paper("doi:10.1/legacy","10.1/legacy","Legacy","Abstract evidence","V","2026-01-01","u",[],"s")
            paper.publication_type_raw="journal-article"; paper.publication_type_source="crossref"
            pm.upsert(db,paper,"now"); db.commit(); db.close()
            with patch("builtins.print") as output: pm.agent_export(config,no_collect=True)
            summary=json.loads(output.call_args.args[0]); queue_path=Path(summary["queue_path"])
            queue=json.loads(queue_path.read_text()); queue["schema_version"]=3
            queue_path.write_text(json.dumps(queue),encoding="utf-8")
            result=self.structured_result(paper.identity); result.pop("recommendation_reason")
            path=root/"legacy.json"
            path.write_text(json.dumps({"run_id":summary["run_id"],"profile_hash":queue["profile_hash"],
                                        "model":"codex-test","results":[result]}),encoding="utf-8")
            self.assertEqual(pm.agent_import(config,path),0)
            db=pm.db_open(root/"papers.sqlite3")
            reason=db.execute("SELECT reasons FROM screenings").fetchone()[0]
            db.close()
            self.assertTrue(reason.startswith("论文证据："))

    def test_agent_import_rejects_partial_exported_queue(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/"research-profile.md").write_text("profile",encoding="utf-8")
            config=root/"config.toml"
            config.write_text('state_dir = "."\nprofile_file = "research-profile.md"\n[[sources]]\nname = "A"\ntype = "crossref"\nissn = "1234"\n',encoding="utf-8")
            db=pm.db_open(root/"papers.sqlite3")
            paper=pm.Paper("doi:10.1/x","10.1/x","A","Abstract","V","2026-01-01","u",[],"s")
            paper.publication_type_raw="journal-article"; paper.publication_type_source="crossref"
            pm.upsert(db,paper,"now"); db.commit(); db.close()
            with patch("builtins.print") as output:
                self.assertEqual(pm.agent_export(config,no_collect=True),0)
            summary=json.loads(output.call_args.args[0]); queue=json.loads(Path(summary["queue_path"]).read_text())
            results=root/"partial.json"
            results.write_text(json.dumps({"run_id":queue["run_id"],"profile_hash":queue["profile_hash"],
                                           "model":"codex-test","results":[]}),encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"complete queue"):
                pm.agent_import(config,results)

    def test_agent_import_is_atomic_and_new_export_abandons_old_job(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/"research-profile.md").write_text("profile",encoding="utf-8")
            config=root/"config.toml"
            config.write_text('state_dir = "."\nprofile_file = "research-profile.md"\n[[sources]]\nname = "A"\ntype = "crossref"\nissn = "1234"\n',encoding="utf-8")
            db=pm.db_open(root/"papers.sqlite3")
            for suffix in ("a","b"):
                paper=pm.Paper(f"doi:10.1/{suffix}",f"10.1/{suffix}",suffix,"Abstract","V","2026-01-01","u",[],"s")
                paper.publication_type_raw="journal-article"; paper.publication_type_source="crossref"
                pm.upsert(db,paper,"now")
            db.commit(); db.close()
            with patch("builtins.print") as output: pm.agent_export(config,no_collect=True)
            first=json.loads(output.call_args.args[0])
            with patch("builtins.print") as output: pm.agent_export(config,no_collect=True)
            second=json.loads(output.call_args.args[0]); queue=json.loads(Path(second["queue_path"]).read_text())
            results=[]
            for index,paper in enumerate(queue["papers"]):
                item=self.structured_result(paper["identity"])
                if index==1: item.pop("reasoning")
                results.append(item)
            result_path=root/"invalid.json"
            result_path.write_text(json.dumps({"run_id":queue["run_id"],"profile_hash":queue["profile_hash"],
                                               "model":"codex-test","results":results}),encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"structured results require"):
                pm.agent_import(config,result_path)
            db=pm.db_open(root/"papers.sqlite3")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM screenings").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT status FROM agent_jobs WHERE run_id=?",(first["run_id"],)).fetchone()[0],"abandoned")
            self.assertEqual(db.execute("SELECT status FROM agent_jobs WHERE run_id=?",(second["run_id"],)).fetchone()[0],"exported")
            db.close()

    def test_agent_export_snapshots_feedback_and_rejects_profile_drift(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); profile=root/"research-profile.md"; profile.write_text("confirmed",encoding="utf-8")
            config=root/"config.toml"
            config.write_text('state_dir = "."\nprofile_file = "research-profile.md"\n[[sources]]\nname = "A"\ntype = "crossref"\nissn = "1234"\n',encoding="utf-8")
            db=pm.db_open(root/"papers.sqlite3")
            paper=pm.Paper("doi:10.1/x","10.1/x","A","Abstract","V","2026-01-01","u",[],"s")
            paper.publication_type_raw="journal-article"; paper.publication_type_source="crossref"
            pm.upsert(db,paper,"now")
            db.execute("INSERT INTO paper_feedback VALUES(?,?,?,?,?,?,?)",
                       (paper.identity,"interested","Direct transfer",1,"read_later","now","now"))
            db.commit(); db.close()
            with patch("builtins.print") as output:
                pm.agent_export(config,no_collect=True)
            summary=json.loads(output.call_args.args[0]); queue=json.loads(Path(summary["queue_path"]).read_text())
            self.assertEqual(queue["schema_version"],5)
            self.assertEqual(queue["evaluation_policy"]["rubric_version"],"evidence-v3")
            self.assertIn("reasoning", queue["evaluation_policy"]["result_fields"])
            self.assertIn("recommendation_reason", queue["evaluation_policy"]["result_fields"])
            self.assertEqual(queue["evaluation_policy"]["minimum_reasoning_characters"]["limitations"],18)
            self.assertEqual(queue["feedback_examples"][0]["reason"],"Direct transfer")
            profile.write_text("unconfirmed edit",encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"confirmed active version"):
                pm.agent_export(config,no_collect=True)

    def test_confirmed_profile_hashes_exact_crlf_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); profile=root/"research-profile.md"
            raw=b"# Profile\r\n\r\nHuman-AI collaboration\r\n"
            profile.write_bytes(raw)
            db=pm.db_open(root/"papers.sqlite3")
            active=pm.confirmed_profile(db,profile)
            self.assertEqual(active["profile_hash"],__import__("hashlib").sha256(raw).hexdigest())
            self.assertEqual(active["content"].encode("utf-8"),raw)
            db.close()

    def test_retried_collection_keeps_papers_new_since_previous_import(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); (root/"research-profile.md").write_text("profile",encoding="utf-8")
            config=root/"config.toml"
            config.write_text('state_dir = "."\nprofile_file = "research-profile.md"\n[[sources]]\nname = "A"\ntype = "crossref"\nissn = "1234"\n',encoding="utf-8")
            db=pm.db_open(root/"papers.sqlite3")
            active=pm.confirmed_profile(db,root/"research-profile.md")
            db.execute("""INSERT INTO agent_jobs(run_id,profile_hash,status,exported_count,imported_count,
              created_at,imported_at,profile_version_id,feedback_snapshot_json)
              VALUES('prior',?,'imported',0,0,'2026-07-16T00:00:00+00:00','2026-07-16T00:10:00+00:00',?,'[]')""",
              (active["profile_hash"],active["id"]))
            paper=pm.Paper("doi:10.1/new","10.1/new","New paper","Abstract","A","2026-07-17","u",[],"A",
                           publication_type_raw="journal-article",publication_type_source="crossref")
            pm.upsert(db,paper,"2026-07-17T00:02:00+00:00")
            db.execute("""INSERT INTO pipeline_runs(run_id,kind,status,started_at,collected_count,candidate_count,
              relevant_count,details_json) VALUES('retry','collection','succeeded','2026-07-17T00:03:00+00:00',1,0,0,'{}')""")
            db.commit(); db.close()
            with patch("builtins.print") as output:
                pm.agent_export(config,no_collect=True,batch_run="retry")
            summary=json.loads(output.call_args.args[0])
            db=pm.db_open(root/"papers.sqlite3")
            roles={row[0] for row in db.execute("SELECT role FROM run_papers WHERE run_id=? AND identity=?",
                                                (summary["run_id"],paper.identity))}
            db.close()
            self.assertIn("new",roles)

if __name__ == "__main__": unittest.main()
