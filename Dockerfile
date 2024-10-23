FROM condaforge/miniforge3 AS base

LABEL author="Remi-Andre Olsen" \
      description="Anglerfish development version" \
      maintainer="remi-andre.olsen@scilifelab.se"

USER root

# Check for arm64 architecture to install minimap2
ARG TARGETARCH
ENV MINIMAP_VERSION=2.26
ENV CONDA_ENV=anglerfish-dev
RUN if [ "$TARGETARCH" = "arm64" ]; then \
      # Install compliation tools for minimap2
      apt-get update;\
      apt-get install -y curl build-essential libz-dev;\
      # Download minimap2
      curl -L https://github.com/lh3/minimap2/archive/refs/tags/v${MINIMAP_VERSION}.tar.gz | tar -zxvf - ;\
      # Compile minimap2
      cd minimap2-${MINIMAP_VERSION};\
      make arm_neon=1 aarch64=1; \
      # Add to path
      mv minimap2 /usr/local/bin/;\
    fi

COPY environment.yml /
COPY requirements.txt /

# Remove minimap2 from environment.yml for arm64
RUN if [ "$TARGETARCH" = "arm64" ]; then \
      grep -v 'minimap2' /environment.yml > /environment.tmp.yml ;\
    else \
      cp /environment.yml /environment.tmp.yml ;\
    fi
RUN cp /environment.yml /environment.tmp.yml

# Add source files to the container
ADD . /usr/src/anglerfish
WORKDIR /usr/src/anglerfish

# Activate the environment
RUN conda env create -y -n $CONDA_ENV --file /environment.tmp.yml && conda clean --all --yes

#####
# Devcontainer
#####
FROM base AS devcontainer

# Useful tools for devcontainer
RUN apt-get update;\
    apt-get install -y git vim
RUN conda init && python -m pip install -e .[dev]
ENV PATH=/opt/conda/envs/$CONDA_ENV/bin:$PATH

#####
# Main
#####
FROM base AS main
RUN conda init && python -m pip install .[dev]
ENV PATH=/opt/conda/envs/$CONDA_ENV/bin:$PATH
