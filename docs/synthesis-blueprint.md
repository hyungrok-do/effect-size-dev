# Stage-two mathematical synthesis blueprint

**Status: design only. No stage-two effect calculations are executed by this release.**

## Required contracts

| Object | Required information |
|---|---|
| Target | Population distribution P*, intervention/exposure contrast, outcome and time, intercurrent-event strategy, causal/associational interpretation, effect scale |
| Source | Native estimand, model or estimating equation, estimated coefficients and nuisance inputs, uncertainty/covariance, sampling/analysis population |
| Mapping | Explicit expression theta* = g_j(eta_j, P*), identification conditions, transport assumptions, approximation rule if used, methodological source |
| Result | Original effect, transformed effect, uncertainty, assumptions, sensitivity range/bounds, data/source provenance and calculation version |

## Separate decisions

| Decision | Mathematical work | Failure output |
|---|---|---|
| Algebra | Show that the available inputs determine the requested functional | Missing inputs; no invented coefficient or baseline |
| Identification | Establish what the design/analysis identifies under stated assumptions | Associational/local parameter retained; no causal relabeling |
| Transport | Justify the relationship between source and target population, including covariate support | Population-specific result or explicit sensitivity/bounds |
| Uncertainty | Propagate joint input uncertainty and dependence | Point identification and recoverable uncertainty reported separately |
| Synthesis | Specify common target, dependence and common/heterogeneous-effect model | Separate compatible sets; no forced overall estimate |

## Example mappings to implement and validate later

For a source conditional outcome mean m_j(a,x), standardization proposes

    mu_j,*(a) = integral m_j(a,x) dP*(x)
    RD_j,* = mu_j,*(1) - mu_j,*(0)
    RR_j,* = mu_j,*(1) / mu_j,*(0)

A causal interpretation requires identification and transport assumptions. Regression fitting alone
is insufficient. Target means/covariate summaries generally do not identify an arbitrary joint P*.

For a conditional logistic odds ratio r(x) and conditional baseline risk p0(x),

    p1(x) = r(x) p0(x) / (1 - p0(x) + r(x) p0(x)).

Standardize the risks before constructing a marginal contrast. A conditional OR alone is insufficient;
plugging a population baseline risk into an adjusted conditional OR is not generally valid.

For survival,

    S_a(t|x) = exp(-Lambda_a(t|x))
    RMST_a,*(tau) = integral_0^tau integral S_a(t|x) dP*(x) dt.

A hazard ratio alone does not identify either an absolute risk or an RMST difference. Baseline survival,
model terms, time horizon, censoring assumptions and the target population must be available.
Competing-risk outcomes require a compatible event definition and the appropriate CIF construction.

For a differentiable mapping, a possible uncertainty calculation is

    Var(theta_hat*) approximately grad(g)' Sigma_eta grad(g).

This requires the full relevant covariance and augmentation for estimated standardization inputs.
Influence-function, bootstrap or posterior propagation may be more appropriate for a given mapping.
Do not silently replace missing covariance by independence. Approximation error and sampling uncertainty
must be distinguished.

Only after compatibility is established should a synthesis model be chosen. Inverse-variance weights
are precision weights, not target-population weights. Random-effects meta-analysis does not turn
different populations/estimands into a specified P* effect. Keep population-specific tables alongside
any later synthesized result.

## Mapping statuses and acceptance criteria

| Status | Required evidence |
|---|---|
| Exact mapping | Derivation and complete inputs under explicitly stated model/design assumptions |
| Approximation | Derivation, approximation regime, sensitivity/error assessment and additional inputs |
| Partial identification | Justified bounds and assumptions used to obtain them |
| Unavailable | Specific missing identification condition or input |

Every implementation needs analytical/reference cases, simulation checks targeted at its assumptions,
and uncertainty/dependence tests. A table-planner citation is a proposed reference, not an automatically
verified derivation. Stage two must retrieve/check that methodological source before calculating.

## Methodological basis

- ICH E9(R1), 2019: estimand, estimator and estimate distinction; intercurrent events.
  https://database.ich.org/sites/default/files/E9-R1_Step4_Guideline_2019_1203.pdf
- Cochrane Handbook, chapter 5: linked reports, original-format extraction and provenance.
  https://www.cochrane.org/authors/handbooks-and-manuals/handbook/current/chapter-05
- Daniel R, Zhang J, Farewell D (2021). Making apples from oranges: comparing noncollapsible effect
  estimators and their standard errors after adjustment for different covariate sets. Biometrical Journal.
  https://doi.org/10.1002/bimj.201900297
- Dahabreh IJ, Robertson SE, Steingrimsson JA, Stuart EA, Hernan MA (2020). Extending inferences from
  a randomized trial to a new target population. Statistics in Medicine.
  https://doi.org/10.1002/sim.8426
- Dahabreh IJ, Petito LC, Robertson SE, Hernan MA, Steingrimsson JA (2020). Toward causally
  interpretable meta-analysis: transporting inferences from multiple randomized trials to a new
  target population. Epidemiology. https://doi.org/10.1097/EDE.0000000000001177
