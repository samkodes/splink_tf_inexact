from __future__ import annotations

import logging
from copy import deepcopy
from typing import TYPE_CHECKING

from .blocking import BlockingRule, block_using_rules_sqls, _sql_gen_where_condition
from .block_from_labels import block_from_labels
from .charts import (
    m_u_parameters_interactive_history_chart,
    match_weights_interactive_history_chart,
    probability_two_random_records_match_iteration_chart,
)
from .comparison import Comparison
from .comparison_level import ComparisonLevel
from .comparison_vector_values import compute_comparison_vector_values_sql
from .constants import LEVEL_NOT_OBSERVED_TEXT
from .exceptions import EMTrainingException
from .expectation_maximisation import expectation_maximisation
from .misc import bayes_factor_to_prob, prob_to_bayes_factor
from .parse_sql import get_columns_used_from_sql

logger = logging.getLogger(__name__)

# https://stackoverflow.com/questions/39740632/python-type-hinting-without-cyclic-imports
if TYPE_CHECKING:
    from .linker import Linker


class EMTrainingSession:
    """Manages training models using the Expectation Maximisation algorithm, and
    holds statistics on the evolution of parameter estimates.  Plots diagnostic charts
    """

    def __init__(
        self,
        linker: Linker,
        blocking_rule_for_training: BlockingRule,
        fix_u_probabilities: bool = False,
        fix_m_probabilities: bool = False,
        fix_probability_two_random_records_match: bool = False,
        comparisons_to_deactivate: list[Comparison] = None,
        comparison_levels_to_reverse_blocking_rule: list[ComparisonLevel] = None,
        estimate_without_term_frequencies: bool = False,
        semi_supervised_rules: list[dict] = None,
        semi_supervised_table = None,
        extend_block_by_ss_table: bool = False
    ):
        logger.info("\n----- Starting EM training session -----\n")

        
        


        self._original_settings_obj = linker._settings_obj
        self._original_linker = linker
        self._training_linker = deepcopy(linker)
        
        
        # ss stuff
        # register ss table and flag whether one exists
        if semi_supervised_table is not None:
            self._semi_supervised_table = self._training_linker.register_semi_supervised_table( semi_supervised_table, overwrite=True)
            # REGISTER IT HERE
        else:
            self._semi_supervised_table = None
        # store ss booleans and ss rules        
        self._semi_supervised_rules = semi_supervised_rules
        self._extend_block_by_ss_table = extend_block_by_ss_table



        self._settings_obj = self._training_linker._settings_obj
        
        if semi_supervised_rules is not None:
            self._settings_obj._retain_matching_columns = True # these may be needed for rules    
        else:
            self._settings_obj._retain_matching_columns = False
        
        self._settings_obj._retain_intermediate_calculation_columns = False
        self._settings_obj._training_mode = True

        if not isinstance(blocking_rule_for_training, BlockingRule):
            blocking_rule_for_training = BlockingRule(blocking_rule_for_training)

        self._settings_obj._blocking_rule_for_training = blocking_rule_for_training
        self._blocking_rule_for_training = blocking_rule_for_training
        self._settings_obj._estimate_without_term_frequencies = (
   #         (semi_supervised_table is None)
   #        and
   #         (semi_supervised_rules is None)
   #        and
            estimate_without_term_frequencies
        )

        if comparison_levels_to_reverse_blocking_rule:
            self._comparison_levels_to_reverse_blocking_rule = (
                comparison_levels_to_reverse_blocking_rule
            )
        else:
            self._comparison_levels_to_reverse_blocking_rule = self._original_settings_obj._get_comparison_levels_corresponding_to_training_blocking_rule(  # noqa
                blocking_rule_for_training.blocking_rule_sql
            )

        self._settings_obj._probability_two_random_records_match = (
            self._blocking_adjusted_probability_two_random_records_match
        )

        self._training_fix_u_probabilities = fix_u_probabilities
        self._training_fix_m_probabilities = fix_m_probabilities
        self._training_fix_probability_two_random_records_match = (
            fix_probability_two_random_records_match
        )

        # Remove comparison columns which are either 'used up' by the blocking rules
        # or alternatively, if the user has manually provided a list to remove,
        # use this instead
        
        
        # edit to make "comparisons_to_deactivate" control EM, but "_comparisons_that_cannot_be_estimated" apply to overall model
        # therefore user can specify the EM, but should not be allowed to specify what affects the overall model
        
        br_cols = get_columns_used_from_sql(
                        blocking_rule_for_training.blocking_rule_sql,
                        self._settings_obj._sql_dialect,
                  )
        
        # if user does not specify, default behaviour is to omit all columns involved in blocking rule from EM
        #if not comparisons_to_deactivate:
        if comparisons_to_deactivate is None:  # allow user to specify empty list! distinguish between None and []!
            comparisons_to_deactivate = []
            for cc in self._settings_obj.comparisons:
                cc_cols = cc._input_columns_used_by_case_statement
                cc_cols = [c.input_name for c in cc_cols]
                if set(br_cols).intersection(cc_cols):
                    comparisons_to_deactivate.append(cc)
        cc_names_to_deactivate = [
            cc._output_column_name for cc in comparisons_to_deactivate
        ]
        
        comparisons_that_cannot_be_estimated = []
        for cc in self._settings_obj.comparisons:
            cc_cols = cc._input_columns_used_by_case_statement
            cc_cols = [c.input_name for c in cc_cols]
            if set(br_cols).intersection(cc_cols):
                comparisons_that_cannot_be_estimated.append(cc)
        cc_names_to_deactivate_model = [
            cc._output_column_name for cc in comparisons_that_cannot_be_estimated    
        ]
        
        self._comparisons_that_cannot_be_estimated: list[
            Comparison
        ] = comparisons_that_cannot_be_estimated

        filtered_ccs_em = [
            cc
            for cc in self._settings_obj.comparisons
            if cc._output_column_name not in cc_names_to_deactivate
        ]

        filtered_ccs_model = [
            cc
            for cc in self._settings_obj.comparisons
            if cc._output_column_name not in cc_names_to_deactivate_model
            
        ]


        self._settings_obj.comparisons = filtered_ccs_em
                
        self._comparisons_that_can_be_estimated = filtered_ccs_model

        self._settings_obj_history = []

        # Add iteration 0 i.e. the starting parameters
        self._add_iteration()

    def _training_log_message(self):
        not_estimated = [
            cc._output_column_name for cc in self._comparisons_that_cannot_be_estimated
        ]
        not_estimated = "".join([f"\n    - {cc}" for cc in not_estimated])

        estimated = [
            cc._output_column_name for cc in self._comparisons_that_can_be_estimated
        ]
        estimated = "".join([f"\n    - {cc}" for cc in estimated])

        if self._training_fix_m_probabilities and self._training_fix_u_probabilities:
            raise ValueError("Can't train model if you fix both m and u probabilites")
        elif self._training_fix_u_probabilities:
            mu = "m probabilities"
        elif self._training_fix_m_probabilities:
            mu = "u probabilities"
        else:
            mu = "m and u probabilities"

        blocking_rule = self._blocking_rule_for_training.blocking_rule_sql

        logger.info(
            f"Estimating the {mu} of the model by blocking on:\n"
            f"{blocking_rule}\n\n"
            "Parameter estimates will be made for the following comparison(s):"
            f"{estimated}\n"
            "\nParameter estimates cannot be made for the following comparison(s)"
            f" since they are used in the blocking rules: {not_estimated}"
        )

    def _comparison_vectors(self):
        self._training_log_message()

        nodes_with_tf = self._original_linker._initialise_df_concat_with_tf()

        sqls = block_using_rules_sqls(self._training_linker)
        for sql in sqls:
            self._training_linker._enqueue_sql(sql["sql"], sql["output_table_name"])

        # repartition after blocking only exists on the SparkLinker
        repartition_after_blocking = getattr(
            self._original_linker, "repartition_after_blocking", False
        )

        if repartition_after_blocking or ( self._extend_block_by_ss_table and self._semi_supervised_table is not None):
            df_blocked = self._training_linker._execute_sql_pipeline([nodes_with_tf])
            input_dataframes = [nodes_with_tf, df_blocked]
        else:
            input_dataframes = [nodes_with_tf]
        
        # semi-supervised stuff: extend blocks before computing comparison vectors if necessary! 
               
        if self._extend_block_by_ss_table and self._semi_supervised_table is not None:
           sqls = self._extend_block_by_table_sqls( self._semi_supervised_table  )
           # debug
           print(sqls)
           for sql in sqls:
               self._training_linker._enqueue_sql(sql["sql"], sql["output_table_name"])
           # have to execute queue now to update blocked df because of blocked df naming issue - can't have two tables with same name in same WITH clause
           df_blocked = self._training_linker._execute_sql_pipeline(input_dataframes)
           input_dataframes = [nodes_with_tf, df_blocked]


        sql = compute_comparison_vector_values_sql(self._settings_obj)
        # debug
        print(sql)
        
        self._training_linker._enqueue_sql(sql, "__splink__df_comparison_vectors")
        return self._training_linker._execute_sql_pipeline(input_dataframes)


    
    def _extend_block_by_table_sqls(self,table):
        # use existing  block_from_labels function, but modify output table name to make distinctly addressible
        labels_sqls = block_from_labels( self._training_linker, 
                                          table.physical_name)
        labels_sqls[-1]["output_table_name"] = "__splink__df_blocked_extension"
        
        # take simple union (which omits duplicates) with existing blocked table
        union_sql = {"sql": "(select * from __splink__df_blocked) UNION (select * from __splink__df_blocked_extension)",
                     "output_table_name": "__splink__df_blocked"}
        
        labels_sqls.append( union_sql)
        return labels_sqls


    def _apply_semi_supervised_labels(self, cvv):
        
        if self._semi_supervised_table is not None:
            
            # use existing  block_from_labels function, but modify output table name for clarity
            labels_sqls = block_from_labels( self._training_linker, 
                                            self._semi_supervised_table.physical_name,    
                                            include_clerical_match_score=True, 
                                            include_colnames=["semi_supervised_match_probability","semi_supervised_omega"])  
            labels_sqls[-1]["output_table_name"] = "__splink__df_semi_supervised_labels"
            for sql in labels_sqls:
                self._training_linker._enqueue_sql(sql["sql"], sql["output_table_name"])
            # we don't actually need the fully blocked table here, but we used this function to get left and right correct 
            # now we can do a simple left join to the existing comparison vector table to label
            
            unique_id_col = self._training_linker._settings_obj._unique_id_column_name
            # check whether source dataset name is required
            
            if self._training_linker._settings_obj._source_dataset_column_name_is_required:
                source_dataset_col = self._training_linker._settings_obj._source_dataset_column_name

                join_condition_l = f"""
                cv.{source_dataset_col}_l = ssl.{source_dataset_col}_l and
                cv.{unique_id_col}_l = ssl.{unique_id_col}_l
                """
                join_condition_r = f"""
                cv.{source_dataset_col}_r = ssl.{source_dataset_col}_r and
                cv.{unique_id_col}_r = ssl.{unique_id_col}_r
                """
            else:
                join_condition_l = f"cv.{unique_id_col}_l = ssl.{unique_id_col}_l"
                join_condition_r = f"cv.{unique_id_col}_r = ssl.{unique_id_col}_r"

            
            table_sql = {"sql": f"""
                          select cv.*,  ssl.semi_supervised_match_probability, ssl.semi_supervised_omega  
                          from __splink__df_comparison_vectors cv
                          left join __splink__df_semi_supervised_labels ssl
                          on ({join_condition_l}) and ({join_condition_r})
                          """,
                         "output_table_name": "__splink__df_comparison_vectors"}
                       
            self._training_linker._enqueue_sql(table_sql["sql"], table_sql["output_table_name"])
            nodes_with_tf = self._original_linker._initialise_df_concat_with_tf()# required for block_from_labels call
            cvv = self._training_linker._execute_sql_pipeline([cvv,nodes_with_tf]) # execute so can update cvv via rules in next step if needed
        
        if self._semi_supervised_rules is not None:
           # messy logic below is needed to deal with case where we have both rules and a table
           if self._semi_supervised_table is not None:
               else_prob_str = "semi_supervised_match_probability"
               else_omega_str = "semi_supervised_omega"
               if self._training_linker._sql_dialect == 'duckdb':
                   select_str = "select * EXCLUDE(semi_supervised_match_probability,semi_supervised_omega) "
               else:
                   select_str = "select * EXCEPT(semi_supervised_match_probability,semi_supervised_omega) "
           else: 
               select_str = "select * "
               else_prob_str = "NULL"
               else_omega_str = "NULL"
           probs_sqls = [f" WHEN ({r['sql']}) THEN {r['match_probability']} " for r in self._semi_supervised_rules]
           omegas_sqls = [f" WHEN ({r['sql']}) THEN {r['omega']} " for r in self._semi_supervised_rules]
           prob_select = "CASE " + " ".join(probs_sqls) + f"ELSE {else_prob_str} END AS semi_supervised_match_probability" 
           omega_select = "CASE " + " ".join(omegas_sqls) + f"ELSE {else_omega_str} END AS semi_supervised_omega" 
           
           rules_sql = {"sql":f"{select_str} , " + prob_select +", " + omega_select + " from __splink__df_comparison_vectors", 
                        "output_table_name": "__splink__df_comparison_vectors" }
           self._training_linker._enqueue_sql(rules_sql["sql"], rules_sql["output_table_name"])
           cvv = self._training_linker._execute_sql_pipeline([cvv])
        
           
        return cvv
            
            

    def _train(self):
        cvv = self._comparison_vectors()
        
        # debug
        cvv_df=cvv.as_pandas_dataframe()
        print(cvv_df.columns)
        
        # semi-supervised stuff: label now!
        if self._semi_supervised_rules is not None or self._semi_supervised_table is not None:
            cvv = self._apply_semi_supervised_labels(cvv)            
            

        # check that the blocking rule actually generates _some_ record pairs,
        # if not give the user a helpful message
        if not cvv.as_record_dict(limit=1):
            br_sql = f"`{self._blocking_rule_for_training.blocking_rule_sql}`"
            raise EMTrainingException(
                f"Training rule {br_sql} resulted in no record pairs.  "
                "This means that in the supplied data set "
                f"there were no pairs of records for which {br_sql} was `true`.\n"
                "Expectation maximisation requires a substantial number of record "
                "comparisons to produce accurate parameter estimates - usually "
                "at least a few hundred, but preferably at least a few thousand.\n"
                "You must revise your training blocking rule so that the set of "
                "generated comparisons is not empty.  You can use "
                "`linker.count_num_comparisons_from_blocking_rule()` to compute "
                "the number of comparisons that will be generated by a blocking rule."
            )

        # Compute the new params, populating the paramters in the copied settings object
        # At this stage, we do not overwrite any of the parameters
        # in the original (main) setting object
        expectation_maximisation(self, cvv)

        rule = self._blocking_rule_for_training.blocking_rule_sql
        training_desc = f"EM, blocked on: {rule}"

        #debug
        print("Adding estimates for comparisons to model:")
        print(self._comparisons_that_can_be_estimated)

        # Add m and u values to original settings
        #for cc in self._settings_obj.comparisons:
        for cc in  self._comparisons_that_can_be_estimated:
            orig_cc = self._original_settings_obj._get_comparison_by_output_column_name(
                cc._output_column_name
            )
            for cl in cc._comparison_levels_excluding_null:
                orig_cl = orig_cc._get_comparison_level_by_comparison_vector_value(
                    cl._comparison_vector_value
                )

                if not self._training_fix_m_probabilities:
                    not_observed = LEVEL_NOT_OBSERVED_TEXT
                    if cl._m_probability == not_observed:
                        orig_cl._add_trained_m_probability(not_observed, training_desc)
                        logger.info(
                            f"m probability not trained for {cc._output_column_name} - "
                            f"{cl.label_for_charts} (comparison vector value: "
                            f"{cl._comparison_vector_value}). This usually means the "
                            "comparison level was never observed in the training data."
                        )
                    else:
                        orig_cl._add_trained_m_probability(
                            cl.m_probability, training_desc
                        )

                if not self._training_fix_u_probabilities:
                    not_observed = LEVEL_NOT_OBSERVED_TEXT
                    if cl._u_probability == not_observed:
                        orig_cl._add_trained_u_probability(not_observed, training_desc)
                        logger.info(
                            f"u probability not trained for {cc._output_column_name} - "
                            f"{cl.label_for_charts} (comparison vector value: "
                            f"{cl._comparison_vector_value}). This usually means the "
                            "comparison level was never observed in the training data."
                        )
                    else:
                        orig_cl._add_trained_u_probability(
                            cl.u_probability, training_desc
                        )

        self._original_linker._em_training_sessions.append(self)

    def _add_iteration(self):
        self._settings_obj_history.append(deepcopy(self._settings_obj))

    @property
    def _blocking_adjusted_probability_two_random_records_match(self):
        orig_prop_m = self._original_settings_obj._probability_two_random_records_match

        adj_bayes_factor = prob_to_bayes_factor(orig_prop_m)

        logger.log(15, f"Original prob two random records match: {orig_prop_m:.3f}")

        comp_levels = self._comparison_levels_to_reverse_blocking_rule
        if not comp_levels:
            comp_levels = self._original_settings_obj._get_comparison_levels_corresponding_to_training_blocking_rule(  # noqa
                self._blocking_rule_for_training.blocking_rule_sql
            )

        for cl in comp_levels:
            adj_bayes_factor = cl._bayes_factor * adj_bayes_factor

            logger.log(
                15,
                f"Increasing prob two random records match using "
                f"{cl.comparison._output_column_name} - {cl.label_for_charts}"
                f" using bayes factor {cl._bayes_factor:,.3f}",
            )

        adjusted_prop_m = bayes_factor_to_prob(adj_bayes_factor)
        logger.log(
            15,
            f"\nProb two random records match adjusted for blocking on "
            f"{self._blocking_rule_for_training.blocking_rule_sql}: "
            f"{adjusted_prop_m:.3f}",
        )
        return adjusted_prop_m

    @property
    def _iteration_history_records(self):
        output_records = []

        for iteration, settings_obj in enumerate(self._settings_obj_history):
            records = settings_obj._parameters_as_detailed_records

            for r in records:
                r["iteration"] = iteration
                r[
                    "probability_two_random_records_match"
                ] = self._settings_obj._probability_two_random_records_match

            output_records.extend(records)
        return output_records

    @property
    def _lambda_history_records(self):
        output_records = []
        for i, s in enumerate(self._settings_obj_history):
            lam = s._probability_two_random_records_match
            r = {
                "probability_two_random_records_match": lam,
                "probability_two_random_records_match_reciprocal": 1 / lam,
                "iteration": i,
            }

            output_records.append(r)
        return output_records

    def probability_two_random_records_match_iteration_chart(self):
        records = self._lambda_history_records
        return probability_two_random_records_match_iteration_chart(records)

    def match_weights_interactive_history_chart(self):
        records = self._iteration_history_records
        return match_weights_interactive_history_chart(
            records, blocking_rule=self._blocking_rule_for_training
        )

    def m_u_values_interactive_history_chart(self):
        records = self._iteration_history_records
        return m_u_parameters_interactive_history_chart(records)

    def _max_change_message(self, max_change_dict):
        message = "Largest change in params was"

        if max_change_dict["max_change_type"] == "probability_two_random_records_match":
            message = (
                f"{message} {max_change_dict['max_change_value']:,.3g} in "
                "probability_two_random_records_match"
            )
        else:
            cl = max_change_dict["current_comparison_level"]
            m_u = max_change_dict["max_change_type"]
            cc_name = cl.comparison._output_column_name

            cl_label = cl.label_for_charts
            level_text = f"{cc_name}, level `{cl_label}`"

            message = (
                f"{message} {max_change_dict['max_change_value']:,.3g} in "
                f"the {m_u} of {level_text}"
            )

        return message

    def _max_change_in_parameters_comparison_levels(self):
        previous_iteration = self._settings_obj_history[-2]
        this_iteration = self._settings_obj_history[-1]
        max_change = -0.1

        max_change_levels = {
            "previous_iteration": None,
            "this_iteration": None,
            "max_change_type": None,
            "max_change_value": None,
        }
        comparisons = zip(previous_iteration.comparisons, this_iteration.comparisons)
        for comparison in comparisons:
            prev_cc = comparison[0]
            this_cc = comparison[1]
            z_cls = zip(prev_cc.comparison_levels, this_cc.comparison_levels)
            for z_cl in z_cls:
                if z_cl[0].is_null_level:
                    continue
                prev_cl = z_cl[0]
                this_cl = z_cl[1]
                change_m = this_cl.m_probability - prev_cl.m_probability
                change_u = this_cl.u_probability - prev_cl.u_probability
                change = max(abs(change_m), abs(change_u))
                change_type = (
                    "m_probability"
                    if abs(change_m) > abs(change_u)
                    else "u_probability"
                )
                change_value = change_m if abs(change_m) > abs(change_u) else change_u
                if change > max_change:
                    max_change = change
                    max_change_levels["prev_comparison_level"] = prev_cl
                    max_change_levels["current_comparison_level"] = this_cl
                    max_change_levels["max_change_type"] = change_type
                    max_change_levels["max_change_value"] = change_value
                    max_change_levels["max_abs_change_value"] = abs(change_value)

        change_probability_two_random_records_match = (
            this_iteration._probability_two_random_records_match
            - previous_iteration._probability_two_random_records_match
        )

        if abs(change_probability_two_random_records_match) > max_change:
            max_change = abs(change_probability_two_random_records_match)
            max_change_levels["prev_comparison_level"] = None
            max_change_levels["current_comparison_level"] = None
            max_change_levels[
                "max_change_type"
            ] = "probability_two_random_records_match"
            max_change_levels[
                "max_change_value"
            ] = change_probability_two_random_records_match
            max_change_levels["max_abs_change_value"] = abs(
                change_probability_two_random_records_match
            )

        max_change_levels["message"] = self._max_change_message(max_change_levels)

        return max_change_levels

    def __repr__(self):
        deactivated_cols = ", ".join(
            [
                cc._output_column_name
                for cc in self._comparisons_that_cannot_be_estimated
            ]
        )
        blocking_rule = self._blocking_rule_for_training.blocking_rule_sql
        return (
            f"<EMTrainingSession, blocking on {blocking_rule}, "
            f"deactivating comparisons {deactivated_cols}>"
        )
